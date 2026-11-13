"""A refused head clears itself when the PR head moves (#3193).

Three identical pre-launch remediation refusals for one head hold the node
under ``admission_refused`` (#3106). The hold names that head, and the first
real drive tick that reads a different PR head from the forge releases it and
reviews the new head once. Only the forge (the PR head) and the agent launch
seams are faked; the store, the overlay, the review stage and git are real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support.driver_fakes import EPIC, NOW, FakeFactsReader, FakeSink, FakeSource
from support.driver_fakes import _facts as node_facts
from support.driver_fakes import _node as node
from support.review_stream import stream_verdict

from kew.config import DispatchConfig, LoggingConfig, ProjectConfig, ReviewConfig
from kew.graph.build import build_graph
from kew.graph.driver.attention import AttentionKind, attention_events
from kew.graph.driver.review_stage import RemediationContext
from kew.graph.driver.tick import advance, summarize
from kew.publication_credentials import CREDENTIAL_PUBLICATION_REFUSAL
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.review_outcome import (
    STAGE_PRE_LAUNCH,
    STATUS_FAILED,
    ReviewOutcome,
    record_review_outcome,
)
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS, run

NODE = "1"
PR = 70


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None, "git required for review-stage tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


@pytest.fixture(autouse=True)
def _forge_pr_body(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The forge's answer to the review stage's PR-body read: an empty body.

    The reviewer stage reads the PR body for declared test edits; that read is
    a ``gh api`` call through ``kew.review_integrity``'s process seam.
    """
    calls: list[list[str]] = []

    def forge(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        assert cmd == ["gh", "api", "--method", "GET", f"repos/o/r/pulls/{PR}"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"body": ""}), stderr=""
        )

    monkeypatch.setattr("kew.review_integrity.run", forge)
    return calls


def _commit(repo: Path, text: str) -> str:
    (repo / "code.py").write_text(text)
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", text], cwd=repo, check=True)
    return run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo on ``main`` with the refused head and the operator's next one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    refused = _commit(repo, "x = 1\n")
    moved = _commit(repo, "x = 2\n")
    return repo, refused, moved


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=3),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        project=ProjectConfig(repo="o/r"),
    )
    config.review.publish_status = False
    config.review.publish_comment = False
    return config


def _forge(head: str) -> FakeFactsReader:
    return FakeFactsReader(
        issue_facts_by_num={
            int(NODE): node_facts(
                int(NODE), pr_number=PR, pr_state="OPEN", head_oid=head
            )
        }
    )


def test_a_refused_head_hold_clears_on_the_first_tick_the_pr_head_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, refused, moved = _repo(tmp_path)
    # The reviewer checkout is cloned from the repository the drive runs in.
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    remediations: list[RemediationContext] = []
    reviews: list[str] = []

    def reviewer(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        reviews.append(ctx.head_sha)
        sidecar = {
            "verdict": "READY",
            "findings": [],
            "acceptance_evidence": [
                {"command": "uv run pytest -q", "output_digest": "sha256:beef"}
            ],
        }
        return stream_verdict(ctx, sidecar)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, NODE, head_sha=refused, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            remediations.append(ctx)
            record_review_outcome(
                ReviewOutcome(
                    pr_number=PR,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification=CREDENTIAL_PUBLICATION_REFUSAL,
                    detail="publication ancestor evidence is missing",
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        def tick(head: str) -> Any:
            return advance(
                EPIC,
                config=config,
                store=store,
                deps=FakeSource(nodes=[node(NODE)], edges=[]),
                facts=_forge(head),  # type: ignore[arg-type]
                dispatch_sink=FakeSink(),
                max_parallel=5,
                now=NOW,
                lock_path=tmp_path / "epic.lock",
                approvers=[],
                repo="o/r",
                base="main",
                reviewer_launcher=reviewer,
                remediator=refuse,
            )

        for _ in range(4):
            tick(refused)
        [hold] = store.list_epic_holds(EPIC)
        # What `kew epic watch` reads: the read-only snapshot, then its events.
        events = attention_events(
            EPIC,
            summarize(
                EPIC,
                build_graph(FakeSource(nodes=[node(NODE)], edges=[]), EPIC),
                config=config,
                store=store,
                facts=_forge(refused),  # type: ignore[arg-type]
                now=NOW,
            ),
        )

        assert len(remediations) == 3
        assert hold.reason.startswith(
            f"admission_refused@{refused[:12]}: credential_publication_refused"
        )
        assert [(e.kind, e.item) for e in events] == [(AttentionKind.HELD, NODE)]
        assert refused[:12] in events[0].reason
        # The operator is told the hold clears itself on a pushed fix, with
        # the unhold as the other way out.
        assert "the hold clears when the PR head moves" in events[0].action
        assert "kew epic unhold" in events[0].action

        tick(moved)

        assert store.list_epic_holds(EPIC) == []
        assert reviews == [moved]
        assert len(remediations) == 3

        # A hold with no head (a dispatch child's) is not head-scoped: a moved
        # head leaves it standing for the operator.
        store.hold_epic_node(EPIC, NODE, "admission_refused: unpriced model")
        tick("f" * 40)
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            "admission_refused: unpriced model"
        ]


def test_a_head_scoped_hold_survives_a_tick_that_reads_the_same_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false positive: an unchanged head (or one the forge did not report)
    must not release the hold, or every tick would relaunch the refusal."""
    repo, refused, _moved = _repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    reason = f"admission_refused@{refused[:12]}: credential_publication_refused: x"
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.hold_epic_node(EPIC, NODE, reason)
        for head in (refused, None):
            advance(
                EPIC,
                config=config,
                store=store,
                deps=FakeSource(nodes=[node(NODE)], edges=[]),
                facts=_forge(head),  # type: ignore[arg-type]
                dispatch_sink=FakeSink(),
                max_parallel=5,
                now=NOW,
                lock_path=tmp_path / "epic.lock",
                approvers=[],
                repo="o/r",
            )
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [reason]


def test_a_send_back_at_the_run_cap_holds_at_once_naming_the_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 32 branch runs the cap refuses every launch alike: hold, don't loop."""
    from kew.publication_credentials import branch_run_cap_refusal

    repo, refused, _moved = _repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    remediations: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, NODE, head_sha=refused, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            remediations.append(ctx)
            record_review_outcome(
                ReviewOutcome(
                    pr_number=PR,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification=CREDENTIAL_PUBLICATION_REFUSAL,
                    detail=str(branch_run_cap_refusal()),
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        for _ in range(3):
            advance(
                EPIC,
                config=config,
                store=store,
                deps=FakeSource(nodes=[node(NODE)], edges=[]),
                facts=_forge(refused),  # type: ignore[arg-type]
                dispatch_sink=FakeSink(),
                max_parallel=5,
                now=NOW,
                lock_path=tmp_path / "epic.lock",
                approvers=[],
                repo="o/r",
                remediator=refuse,
            )
        [hold] = store.list_epic_holds(EPIC)
        events = attention_events(
            EPIC,
            summarize(
                EPIC,
                build_graph(FakeSource(nodes=[node(NODE)], edges=[]), EPIC),
                config=config,
                store=store,
                facts=_forge(refused),  # type: ignore[arg-type]
                now=NOW,
            ),
        )

    assert len(remediations) == 1
    assert hold.reason.startswith(f"admission_refused@{refused[:12]}:")
    assert [(e.kind, e.item) for e in events] == [(AttentionKind.HELD, NODE)]
    assert "the 32 runs the publication guard reads" in events[0].reason
    assert events[0].reason.endswith(
        "options: push a fix, merge by hand, or close the PR"
    )


def test_a_deep_lineage_refusal_is_not_read_as_the_run_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other bounded walks share the cap's reason but not its diagnosis: such a
    refusal keeps its own detail and is retried, never held as the run cap."""
    from kew.publication_credentials import CredentialPublicationRefused

    repo, refused, _moved = _repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    remediations: list[RemediationContext] = []
    detail = str(CredentialPublicationRefused("publication_lineage_too_deep"))
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, NODE, head_sha=refused, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            remediations.append(ctx)
            record_review_outcome(
                ReviewOutcome(
                    pr_number=PR,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification=CREDENTIAL_PUBLICATION_REFUSAL,
                    detail=detail,
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        def tick() -> None:
            advance(
                EPIC,
                config=config,
                store=store,
                deps=FakeSource(nodes=[node(NODE)], edges=[]),
                facts=_forge(refused),  # type: ignore[arg-type]
                dispatch_sink=FakeSink(),
                max_parallel=5,
                now=NOW,
                lock_path=tmp_path / "epic.lock",
                approvers=[],
                repo="o/r",
                remediator=refuse,
            )

        tick()
        assert store.list_epic_holds(EPIC) == []
        for _ in range(3):
            tick()
        holds = [h.reason for h in store.list_epic_holds(EPIC)]

    assert len(remediations) == 3
    assert len(holds) == 1
    assert "exceeds the bounded policy walk" in holds[0]
    assert "branch_run_cap" not in holds[0]
