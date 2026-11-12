"""The reviewer rules on proposed Behavior changes lines, and a remediation never
repeats at a head it did not move (#3475).

On the night of 2026-11-05 five release nodes each ran 25 to 29 remediations
at one head. Every verdict was NEEDS_CHANGES only because edited tests had no
Behavior changes line. The implementer had proposed one for each, but the
reviewer could only ask for them, and each remediation could only write the
proposals again and push nothing. The head never moved, no review ran, and the
drive launched the next remediation two minutes later.

These pin the fix at its observable edges: the PR body as the forge holds it,
the review round kew records, the hold it writes, and the remediations it
launches.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.driver_fakes import EPIC
from support.gh_fakes import GhProcessBoundary
from support.held_out_log import report_line
from support.review_stream import stream_verdict

from kew import subprocess_utils
from kew import test_integrity as integrity
from kew.audit import Actor, emit
from kew.behavior_declarations import drop_proposed_lines, propose_lines
from kew.config import ApprovalConfig, DispatchConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import (
    DECLARATION_RECORD_FAILED_HOLD_PREFIX,
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
    REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
    holds_node_out_of_frontier,
    remediation_no_change_hold_reason,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.merge_verify import (
    behavior_change_lines,
    declared_behavior_changes,
    held_out_report,
)
from kew.pr_text import proposed_section_lines
from kew.review_contract import parse_verdict_sidecar
from kew.review_delta import REVIEW_MODE_OPERATOR_SEND_BACK
from kew.review_outcome import STATUS_COMPLETED, ReviewOutcome, record_review_outcome
from kew.run_store import REVIEW_REMEDIATION_RUN_KIND, RunRecord, SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS

ITEM = "3457"
PR = 3472
NOW = datetime(2026, 11, 5, 3, 12, tzinfo=UTC)
WIDGET = "tests/test_widget.py::test_widget"
GADGET = "tests/test_widget.py::test_gadget"
WIDGET_REASON = "the widget now counts the new slot, as the issue asks"
GADGET_REASON = "the gadget is simply faster now"
WIDGET_LINE = f"- {WIDGET} — {WIDGET_REASON}"
GADGET_LINE = f"- {GADGET} — {GADGET_REASON}"
APPROVER = "operator"


def _proposed_body(*lines: str) -> str:
    """A PR body as kew publishes it: the implementer's lines, fenced, under
    **Proposed behavior changes** and nowhere else."""
    return (
        "Closes #3457\n\n**Cause:** the widget miscounts\n\n"
        "**Proposed behavior changes**\n\n"
        "The agent's own lines, verbatim. They declare nothing.\n\n"
        "```\n" + "".join(f"{line}\n" for line in lines) + "```\n"
    )


class _Forge:
    """The PR-body REST calls kew makes: a read and a PATCH, on one PR."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.patches = 0
        self.refuse_writes = False
        self.refuse_reads = False
        self.unreadable = 0
        """How many of the next reads fail, as a ``gh`` hiccup does."""

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] != ["gh", "api"] or f"repos/o/r/pulls/{PR}" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown")
        if "PATCH" in cmd:
            if self.refuse_writes:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="403")
            self.patches += 1
            self.body = cmd[cmd.index("-f") + 1].removeprefix("body=")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        if self.refuse_reads:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="502")
        if self.unreadable:
            self.unreadable -= 1
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="502")
        payload = json.dumps({"body": self.body})
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> _Forge:
    fake = _Forge(_proposed_body(WIDGET_LINE, GADGET_LINE))
    monkeypatch.setitem(TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(fake, tool_paths=TOOL_PATHS),
    )
    return fake


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _edited_tests_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose feature head edits both existing tests."""
    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")
    repo = tmp_path / "project"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    test_file = repo / "tests" / "test_widget.py"
    test_file.write_text(
        "def test_widget():\n    assert widget() == 3\n\n\n"
        "def test_gadget():\n    assert gadget() < 2\n"
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    test_file.write_text(
        "def test_widget():\n    assert widget() == 4\n\n\n"
        "def test_gadget():\n    assert gadget() < 1\n"
    )
    _git(repo, "commit", "--quiet", "-am", "edit both expectations")
    return repo


def _facts(head: str) -> NodeFacts:
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


def _tick(
    store: Any,
    tmp_path: Path,
    head: str,
    *,
    repo_root: Path | None = None,
    launcher: Any = None,
    remediations: list[RemediationContext] | None = None,
    approvers: tuple[str, ...] = (APPROVER,),
) -> Any:
    """One drive pass at *head*, with APPROVER the one ``[approval]`` approver
    unless *approvers* says otherwise."""
    latest = store.latest_review_round(ITEM)
    facts = _facts(head)
    status = overlay_status(
        facts,
        [],
        reserved=False,
        held_reason=next((h.reason for h in store.list_epic_holds(EPIC)), None),
        review=ReviewOverlay(
            enabled=True,
            reserved_kind=None,
            round_count=store.review_round_count(ITEM),
            latest_verdict=latest.verdict if latest else None,
            latest_head_sha=latest.head_sha if latest else None,
            current_head_sha=head,
        ),
    )
    return drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=DispatchConfig(
            review=ReviewConfig(
                adversarial=True,
                blocking=True,
                max_rounds=8,
                publish_status=False,
                publish_comment=False,
            ),
            logging=LoggingConfig(dir=str(tmp_path / "logs")),
            approval=ApprovalConfig(approvers=list(approvers)),
        ),
        store=store,
        repo="o/r",
        repo_root=repo_root or tmp_path,
        base="main",
        reviewer_launcher=launcher,
        remediator=(remediations if remediations is not None else []).append,
        now=NOW,
    )


def _reviewer(declarations: list[dict[str, str]], prompts: list[str]) -> Any:
    """A reviewer that answers the undeclared-edits flag and rules on each
    proposal as *declarations* say, recording the prompt it was shown."""

    def launch(ctx: Any) -> Any:
        prompts.append(ctx.prompt)
        return stream_verdict(
            ctx,
            {
                "verdict": "READY",
                "findings": [],
                "acceptance_executed": True,
                "acceptance_evidence": [
                    {"command": "uv run pytest tests/", "output_digest": "2 passed"}
                ],
                "test_changes": [
                    {
                        "flag": integrity.FLAG_UNDECLARED_TEST_EDITS,
                        "answer": "ACCEPTED",
                        "reason": "every edited test has an accepted proposal",
                    }
                ],
                "declarations": declarations,
            },
        )

    return launch


_ACCEPT_WIDGET = {
    "node_id": WIDGET,
    "answer": "accepted",
    "reason": "the base copy fails only on the slot count the issue changes",
}
_ACCEPT_GADGET = {
    "node_id": GADGET,
    "answer": "accepted",
    "reason": "the base copy fails only on the new timing bound",
}
_REJECT_GADGET = {
    "node_id": GADGET,
    "answer": "rejected",
    "reason": "the issue never asks for a faster gadget",
}


def test_a_review_that_accepts_every_proposal_is_ready_and_the_body_declares_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_ACCEPT_WIDGET, _ACCEPT_GADGET], prompts),
        )
        latest = store.latest_review_round(ITEM)

    [prompt] = prompts
    assert WIDGET_LINE in prompt and GADGET_LINE in prompt, "shown to rule on"
    assert "`declarations` entry for each proposed line" in prompt
    assert outcome.held == []
    assert latest is not None and latest.verdict == "READY"
    assert declared_behavior_changes(forge.body) == {WIDGET, GADGET}
    assert [ln for ln in behavior_change_lines(forge.body) if ln.strip()] == [
        WIDGET_LINE,
        GADGET_LINE,
    ], "exactly the accepted lines, each with the proposal's own reason"
    assert proposed_section_lines(forge.body) == [WIDGET_LINE, GADGET_LINE]


def test_a_rejected_proposal_stays_out_of_the_body_and_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_ACCEPT_WIDGET, _REJECT_GADGET], []),
        )
        latest = store.latest_review_round(ITEM)

    assert outcome.held == []
    assert latest is not None and latest.verdict == "NEEDS_CHANGES"
    assert declared_behavior_changes(forge.body) == {WIDGET}
    assert GADGET_LINE not in behavior_change_lines(forge.body)
    findings = parse_verdict_sidecar(Path(latest.sidecar_path).read_bytes()).findings
    named = [f for f in findings if GADGET in f.summary]
    assert len(named) == 1
    assert "the issue never asks for a faster gadget" in named[0].summary
    assert named[0].kind is None, "an ordinary finding, never a declaration"


def test_the_held_out_gate_excuses_only_the_accepted_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """Both tests were proposed; only the accepted one is declared, so the
    merge's held-out gate still fails on the other — a proposal the reviewer
    never accepted excuses nothing."""
    log = (
        f"{report_line(WIDGET)}kew-held-out: exit 1 seconds 1\n"
        f"{report_line(GADGET)}kew-held-out: exit 1 seconds 1\n"
    )
    before = held_out_report(
        log, runs=2, declared=declared_behavior_changes(forge.body)
    )
    assert set(before.undeclared) == {WIDGET, GADGET}, "proposals declare nothing"

    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_ACCEPT_WIDGET, _REJECT_GADGET], []),
        )

    after = held_out_report(log, runs=2, declared=declared_behavior_changes(forge.body))
    assert after.outcome == "failed"
    assert after.undeclared == (GADGET,)


def test_a_ready_whose_accepted_lines_are_not_written_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.refuse_writes = True
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_ACCEPT_WIDGET, _ACCEPT_GADGET], []),
        )
        latest = store.latest_review_round(ITEM)
        reasons = [h.reason for h in store.list_epic_holds(EPIC)]

    assert latest is None, "no READY stands without its lines in the body"
    assert outcome.held == [ITEM]
    [reason] = reasons
    assert reason.startswith(f"{DECLARATION_RECORD_FAILED_HOLD_PREFIX}: ")
    assert f"PR #{PR}" in reason
    assert holds_node_out_of_frontier(reason)
    assert declared_behavior_changes(forge.body) == frozenset()


# --- No second remediation at a head the last one did not move ---------------

HEAD = "1703f0710218" + "a" * 28


def _standing_round(store: Any, tmp_path: Path) -> None:
    """A NEEDS_CHANGES round at HEAD with one ordinary finding."""
    sidecar = tmp_path / "logs" / "round-1.sidecar.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "medium",
                        "location": "src/app/widget.py:3",
                        "summary": "the widget still miscounts an empty slot",
                        "evidence": {
                            "command": "uv run pytest",
                            "output_digest": "1 failed",
                        },
                    }
                ],
            }
        )
    )
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=HEAD,
        verdict="NEEDS_CHANGES",
        sidecar_digest="d",
        sidecar_path=str(sidecar),
    )


def _remediation_ended(store: Any, run_id: str) -> None:
    """A review remediation that finished cleanly after the latest round and
    recorded no outcome of its own — the night's runs, which pushed nothing."""
    started = store.latest_review_round(ITEM).recorded_at + timedelta(seconds=1)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=PR,
            item_id=ITEM,
            branch="agent/issue-3457",
            status="success",
            exit_code=0,
            started_at=started,
            finished_at=started + timedelta(seconds=89),
            run_kind=REVIEW_REMEDIATION_RUN_KIND,
        )
    )


def test_a_remediation_that_pushed_nothing_is_never_launched_again_at_that_head(
    tmp_path: Path, forge: _Forge
) -> None:
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _standing_round(store, tmp_path)
        _tick(store, tmp_path, HEAD, remediations=launched)
        assert [c.head_sha for c in launched] == [HEAD], "the first one runs"
        _remediation_ended(store, "rem-1")

        outcome = _tick(store, tmp_path, HEAD, remediations=launched)
        assert outcome.held == [ITEM]
        for _ in range(3):
            _tick(store, tmp_path, HEAD, remediations=launched)

        reasons = [h.reason for h in store.list_epic_holds(EPIC)]

    assert len(launched) == 1, "the run count stops rising"
    [reason] = reasons
    assert reason.startswith(f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: ")
    assert HEAD[:12] in reason and "round 1" in reason
    assert holds_node_out_of_frontier(reason)


def test_a_remediation_that_changed_the_proposals_earns_one_re_review_and_no_more(
    tmp_path: Path, forge: _Forge
) -> None:
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _standing_round(store, tmp_path)
        _tick(store, tmp_path, HEAD, remediations=launched)
        assert len(launched) == 1
        # The remediation pushed nothing, but proposed a line in the body.
        assert propose_lines("o/r", PR, [f"- {WIDGET} — a better reason"])
        _remediation_ended(store, "rem-1")

        outcome = _tick(store, tmp_path, HEAD, remediations=launched)
        assert len(launched) == 1, "the head is reviewed, not remediated again"
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []

        # That one re-review of the same head still finds NEEDS_CHANGES.
        _standing_round(store, tmp_path)
        outcome = _tick(store, tmp_path, HEAD, remediations=launched)
        reasons = [h.reason for h in store.list_epic_holds(EPIC)]

    assert len(launched) == 1
    assert outcome.held == [ITEM]
    [reason] = reasons
    assert reason.startswith(f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: ")
    assert "round 2" in reason


def test_a_remediation_launched_over_an_unread_body_is_held_not_re_reviewed(
    tmp_path: Path, forge: _Forge
) -> None:
    """With the body unread at launch kew has no digest to compare, so a
    block that reads differently later is no evidence the remediation moved
    anything: the node is held, not re-reviewed."""
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _standing_round(store, tmp_path)
        forge.refuse_reads = True
        _tick(store, tmp_path, HEAD, remediations=launched)
        assert len(launched) == 1

        forge.refuse_reads = False
        forge.body = _proposed_body(f"- {WIDGET} — a better reason", GADGET_LINE)
        _remediation_ended(store, "rem-1")
        outcome = _tick(store, tmp_path, HEAD, remediations=launched)
        reasons = [h.reason for h in store.list_epic_holds(EPIC)]

    assert len(launched) == 1
    assert outcome.held == [ITEM]
    [reason] = reasons
    assert reason.startswith(f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: ")


def test_a_remediation_whose_push_moved_the_head_is_reviewed_not_held(
    tmp_path: Path, forge: _Forge
) -> None:
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _standing_round(store, tmp_path)
        _tick(store, tmp_path, HEAD, remediations=launched)
        _remediation_ended(store, "rem-1")

        outcome = _tick(store, tmp_path, "c" * 40, remediations=launched)
        holds = store.list_epic_holds(EPIC)

    assert len(launched) == 1
    assert outcome.held == []
    assert holds == []


def test_a_remediations_proposals_replace_its_earlier_line_for_the_same_test(
    forge: _Forge,
) -> None:
    assert propose_lines("o/r", PR, [f"- {WIDGET} — a better reason"])

    assert proposed_section_lines(forge.body) == [
        f"- {WIDGET} — a better reason",
        GADGET_LINE,
    ]
    assert declared_behavior_changes(forge.body) == frozenset()
    assert forge.body.startswith("Closes #3457\n")
    assert propose_lines("o/r", PR, [f"- {WIDGET} — a better reason"])
    assert forge.patches == 1, "a block already holding the lines is not rewritten"


def test_a_ruling_with_no_reason_declares_nothing_and_a_conflict_is_a_rejection() -> (
    None
):
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [
                {
                    "severity": "low",
                    "location": "src/app/widget.py:1",
                    "summary": "a typo",
                    "evidence": {"command": "rg typo", "output_digest": "1 hit"},
                }
            ],
            "declarations": [
                {"node_id": WIDGET, "answer": "accepted", "reason": "  "},
                {"node_id": GADGET, "answer": "accepted", "reason": "matches"},
                {"node_id": GADGET, "answer": "rejected", "reason": "not asked"},
                {"node_id": WIDGET, "answer": "maybe", "reason": "unsure"},
            ],
        }
    )

    assert [(r.node_id, r.accepted) for r in parsed.declarations] == [(GADGET, False)]
    assert [f.summary for f in parsed.findings if GADGET in f.summary] == [
        f"Proposed Behavior changes line for {GADGET} rejected: not asked"
    ]


# --- A person's proposal after a no-change hold earns one re-review ----------


def _declaration_round(
    store: Any,
    tmp_path: Path,
    head: str,
    node_ids: tuple[str, ...] = (WIDGET,),
    *,
    review_mode: str | None = None,
) -> None:
    """A NEEDS_CHANGES round at *head* whose one finding asks only for a
    Behavior changes line for each of *node_ids* — WIDGET by default — filed
    in *review_mode* (an operator send-back, say)."""
    sidecar = tmp_path / "logs" / "round-8.sidecar.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "medium",
                        "location": "tests/test_widget.py:1",
                        "summary": f"{', '.join(node_ids)} has no line; a "
                        "declaration line is the whole fix",
                        "evidence": {"command": "uv run pytest", "output_digest": "1"},
                        "kind": "declaration",
                        "node_ids": list(node_ids),
                    }
                ],
            }
        )
    )
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head,
        verdict="NEEDS_CHANGES",
        sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        sidecar_path=str(sidecar),
        review_mode=review_mode,
    )


def _no_op_remediation(
    store: Any,
    tmp_path: Path,
    head: str,
    run_id: str,
    *,
    started: datetime | None = None,
    published: str | None = None,
    completed: bool = True,
    tree: str = "",
) -> None:
    """A remediation that ran to success at *head* and changed nothing: its
    run row and the completed outcome publishing the head it was cut at, or
    *published* for one whose push the PR head read has not caught up with.

    It starts at the round's own moment unless *started* says otherwise, so
    a release a person records after it, and the re-review round that
    follows, both come later. With *completed* False it records no outcome,
    which proves nothing changed or not (#3475). A *tree* records the one
    tree kew's base merge handed the agent and the agent published, so a
    *published* head that merge alone moved is still no change (#3276)."""
    started = started or store.latest_review_round(ITEM).recorded_at
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=PR,
            item_id=ITEM,
            branch="agent/issue-3457",
            status="success",
            exit_code=0,
            started_at=started,
            finished_at=started,
            run_kind=REVIEW_REMEDIATION_RUN_KIND,
        )
    )
    if not completed:
        return
    record_review_outcome(
        ReviewOutcome(
            pr_number=PR,
            status=STATUS_COMPLETED,
            stage="runner",
            classification="completed",
            branch="agent/issue-3457",
            head_sha=published or head,
            item_id=ITEM,
            cut_head_sha=head,
            integrated_tree=tree,
            published_tree=tree,
        ),
        store=store,
        config=DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "logs"))),
        notify=False,
    )


def _unhold(store: Any, head: str | None, actor: str = APPROVER) -> str:
    """*actor*'s ``kew epic unhold``: the row removed, the release attested
    with the reason it released and the head the PR stood at — none for a
    *head* of None, as a release recorded before kew kept one."""
    hold = store.remove_epic_hold(EPIC, ITEM)
    assert hold is not None
    emit(
        store,
        "epic_node_unhold",
        actor=Actor(type="human", id=actor),
        issue_num=int(ITEM),
        item_id=ITEM,
        repo="o/r",
        payload={
            "epic_ref": EPIC,
            "node": ITEM,
            "reason": hold.reason,
            **({} if head is None else {"pr_head": head}),
        },
    )
    return str(hold.reason)


def _held_reason(store: Any) -> str:
    [hold] = store.list_epic_holds(EPIC)
    return str(hold.reason)


def test_an_operator_proposal_after_a_no_change_hold_earns_one_re_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """#3466's night: a declaration-only round's remediation proposed nothing
    and the node was held ``remediation_no_change`` at H. The person proposes
    the line in the PR body and releases it: H is reviewed once, not
    remediated, and the reviewer's ruling on the new line decides the round. A
    second release with no new body change buys no second re-review."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    rejects = _reviewer(
        [_ACCEPT_GADGET, {**_REJECT_GADGET, "node_id": WIDGET}], prompts
    )

    def tick() -> Any:
        return _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=rejects,
            remediations=launched,
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)
        tick()
        assert len(launched) == 1, "the round's first remediation runs"
        _no_op_remediation(store, tmp_path, head, "rem-1")
        assert tick().held == [ITEM]
        assert _held_reason(store).startswith(f"{REMEDIATION_NO_CHANGE_HOLD_PREFIX}: ")

        # The person proposes the line the round asked for, then releases.
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        outcome = tick()
        assert len(launched) == 1, "reviewed, never remediated on their block"
        [prompt] = prompts
        assert WIDGET_LINE in prompt, "the reviewer is shown the new line to rule on"
        re_review = store.latest_review_round(ITEM)
        assert re_review is not None and re_review.head_sha == head
        assert re_review.round == 2 and re_review.verdict == "NEEDS_CHANGES"
        assert outcome.held == []

        # The reviewer rejected it: that ruling decides, the node is held.
        assert tick().held == [ITEM]
        reason = _held_reason(store)
        assert reason.startswith(f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: ")
        assert "round 2" in reason and "propose lines in the PR body" in reason
        assert len(launched) == 1

        # A second release with the body unchanged: one more remediation.
        _unhold(store, head)
        tick()
        assert len(prompts) == 1, "no second re-review"
        assert [c.head_sha for c in launched] == [head, head]
        assert store.latest_review_round(ITEM).round == 2


def test_an_accepted_operator_proposal_after_a_no_change_hold_readies_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    accepts = _reviewer([_ACCEPT_WIDGET, _ACCEPT_GADGET], [])
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)
        _tick(store, tmp_path, head, repo_root=repo, remediations=launched)
        _no_op_remediation(store, tmp_path, head, "rem-1")
        _tick(store, tmp_path, head, repo_root=repo, remediations=launched)

        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        outcome = _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=accepts,
            remediations=launched,
        )
        latest = store.latest_review_round(ITEM)

    assert len(launched) == 1
    assert outcome.held == []
    assert latest is not None and latest.verdict == "READY"
    assert declared_behavior_changes(forge.body) == {WIDGET, GADGET}


def test_a_release_with_an_unchanged_proposed_block_buys_a_remediation(
    tmp_path: Path, forge: _Forge
) -> None:
    """The block proposes no line for WIDGET, the round's one test, and the
    release did not change it: one more remediation, as before #3493."""
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, HEAD)
        _tick(store, tmp_path, HEAD, remediations=launched)
        _no_op_remediation(store, tmp_path, HEAD, "rem-1")
        assert _tick(store, tmp_path, HEAD, remediations=launched).held == [ITEM]

        _unhold(store, HEAD)
        outcome = _tick(store, tmp_path, HEAD, remediations=launched)

    assert [c.head_sha for c in launched] == [HEAD, HEAD]
    assert outcome.remediating == [ITEM]


DOOHICKEY = "tests/test_widget.py::test_doohickey"


def _drive_tick(
    store: Any,
    tmp_path: Path,
    head: str,
    repo: Path,
    *,
    prompts: list[str],
    launched: list[RemediationContext],
) -> Any:
    """One pass with a reviewer that rejects WIDGET's proposal, so any
    re-review shows up in *prompts* and any remediation in *launched*."""
    return _tick(
        store,
        tmp_path,
        head,
        repo_root=repo,
        launcher=_reviewer([{**_REJECT_GADGET, "node_id": WIDGET}], prompts),
        remediations=launched,
    )


def _held_after_one_no_op(
    store: Any,
    tmp_path: Path,
    head: str,
    tick: Any,
    launched: list[RemediationContext],
) -> None:
    """The round's first remediation runs and changes nothing; the node is
    held ``remediation_no_change`` at *head*."""
    tick()
    assert len(launched) == 1, "the round's first remediation runs"
    _no_op_remediation(store, tmp_path, head, "rem-1")
    assert tick().held == [ITEM]
    assert _held_reason(store).startswith(f"{REMEDIATION_NO_CHANGE_HOLD_PREFIX}: ")


def test_an_operator_line_proposed_before_the_remediation_earns_a_re_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """#3467's sequence: the operator proposes WIDGET's line before the
    round's remediation launches. That remediation is never sent after the
    same test, so it proposes nothing new and the node is held with the
    operator's line already in the block kew recorded. The release changes
    nothing in the body, yet earns the one re-review: the block holds a line
    for every test the round asks about, and only a reviewer can rule on it.
    The operator's line is never replaced."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)
        assert propose_lines("o/r", PR, [WIDGET_LINE]), "the operator's line"

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, head, repo, prompts=prompts, launched=launched
            )

        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        assert launched[0].proposal is None, "no brief sends it after WIDGET"
        _unhold(store, head)
        tick()
        latest = store.latest_review_round(ITEM)

    assert len(launched) == 1, "re-reviewed, never remediated on their line"
    [prompt] = prompts
    assert WIDGET_LINE in prompt, "the reviewer rules on the operator's line"
    assert latest is not None and (latest.round, latest.head_sha) == (2, head)
    assert WIDGET_LINE in proposed_section_lines(forge.body)


def test_a_proposal_brief_asks_only_for_tests_no_proposed_line_answers(
    tmp_path: Path, forge: _Forge
) -> None:
    """The round asks about WIDGET and DOOHICKEY and the block already holds
    WIDGET's line: the brief names DOOHICKEY alone."""
    forge.body = _proposed_body(WIDGET_LINE)
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, HEAD, (WIDGET, DOOHICKEY))
        _tick(store, tmp_path, HEAD, remediations=launched)

    [ctx] = launched
    assert ctx.proposal is not None and ctx.proposal.node_ids == (DOOHICKEY,)


def test_an_unreadable_body_after_a_release_waits_a_tick_instead_of_remediating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """One failed ``gh`` read right after the release is not "no change": the
    node waits, and the next pass reads the body and re-reviews the head."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, head, repo, prompts=prompts, launched=launched
            )

        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        forge.unreadable = 1
        waited = tick()
        assert (len(launched), prompts, waited.held) == (1, [], [])
        assert store.list_epic_holds(EPIC) == []

        tick()

    assert len(launched) == 1, "the release is never spent on a remediation"
    assert len(prompts) == 1


def test_a_failed_read_at_launch_still_lets_a_release_with_new_lines_re_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """The body could not be read as the remediation launched, so kew held no
    record of the block. The hold reads it then, and a person's line for one
    of the round's two tests is a change the release's re-review rules on —
    the hold message's promise."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head, (WIDGET, DOOHICKEY))

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, head, repo, prompts=prompts, launched=launched
            )

        forge.unreadable = 1
        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        tick()

    assert len(launched) == 1
    assert len(prompts) == 1


def test_a_release_by_someone_who_is_not_an_approver_buys_a_remediation(
    tmp_path: Path, forge: _Forge
) -> None:
    """The block answers the round, but the release is not an approver's: it
    buys what a release bought before, one more remediation."""
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, HEAD)

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, HEAD, tmp_path, prompts=prompts, launched=launched
            )

        _held_after_one_no_op(store, tmp_path, HEAD, tick, launched)
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, HEAD, actor="bystander")
        outcome = tick()

    assert prompts == []
    assert [c.head_sha for c in launched] == [HEAD, HEAD]
    assert outcome.remediating == [ITEM]


def test_a_no_progress_hold_records_the_block_its_release_is_compared_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """A remediation with no completed outcome pushed nothing: the node is held
    ``remediation_no_progress``, whose message says proposing lines and
    releasing earns a re-review. It does."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, head, repo, prompts=prompts, launched=launched
            )

        tick()
        _no_op_remediation(store, tmp_path, head, "rem-1", completed=False)
        assert tick().held == [ITEM]
        assert _held_reason(store).startswith(
            f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: "
        )
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        tick()

    assert len(launched) == 1
    assert len(prompts) == 1


def test_a_release_older_than_the_hold_earns_no_re_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """The release bought a remediation, which changed nothing, and the node
    was held again. That hold cleared without a new release: the old one
    answers a hold set before it, never this one."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, head, repo, prompts=prompts, launched=launched
            )

        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        _unhold(store, head)
        tick()
        assert len(launched) == 2, "the release buys one more remediation"
        _no_op_remediation(store, tmp_path, head, "rem-2")
        assert tick().held == [ITEM]

        assert propose_lines("o/r", PR, [WIDGET_LINE])
        assert store.remove_epic_hold(EPIC, ITEM) is not None
        outcome = tick()

    assert prompts == []
    assert outcome.held == [ITEM]


def test_a_hold_with_no_record_at_its_round_earns_its_release_no_re_review(
    tmp_path: Path, forge: _Forge
) -> None:
    """kew recorded the block for round 1's hold. Round 2 at the same head is
    held with no record of its own (a hold written before kew kept one): its
    release is compared with nothing, and buys one more remediation."""
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, HEAD)

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, HEAD, tmp_path, prompts=prompts, launched=launched
            )

        _held_after_one_no_op(store, tmp_path, HEAD, tick, launched)
        assert store.remove_epic_hold(EPIC, ITEM) is not None
        _declaration_round(store, tmp_path, HEAD)
        store.hold_epic_node(
            EPIC, ITEM, remediation_no_change_hold_reason(("WIDGET has no line",))
        )
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, HEAD)
        tick()

    assert prompts == []
    assert [c.head_sha for c in launched] == [HEAD, HEAD]


def test_a_remediation_since_the_release_spends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """The release bought a remediation whose push this pass's head read has
    not caught up with. The release is spent: lines proposed after it earn
    the old head no re-review."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick() -> Any:
            return _drive_tick(
                store, tmp_path, head, repo, prompts=prompts, launched=launched
            )

        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        _unhold(store, head)
        tick()
        assert len(launched) == 2
        _no_op_remediation(
            store,
            tmp_path,
            head,
            "rem-2",
            started=datetime.now(UTC),
            published="c" * 40,
        )
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        tick()

    assert prompts == []


def _asks_for_widget_again(prompts: list[str]) -> Any:
    """A reviewer that rules on no proposed line and files WIDGET's line as a
    ``kind: "declaration"`` finding again, recording the prompt it was shown."""

    def launch(ctx: Any) -> Any:
        prompts.append(ctx.prompt)
        return stream_verdict(
            ctx,
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "medium",
                        "location": "tests/test_widget.py:1",
                        "summary": f"{WIDGET} still has no Behavior changes line",
                        "evidence": {"command": "uv run pytest", "output_digest": "1"},
                        "kind": "declaration",
                        "node_ids": [WIDGET],
                    }
                ],
                "acceptance_executed": True,
                "acceptance_evidence": [
                    {"command": "uv run pytest tests/", "output_digest": "2 passed"}
                ],
                "test_changes": [
                    {
                        "flag": integrity.FLAG_UNDECLARED_TEST_EDITS,
                        "answer": "ACCEPTED",
                        "reason": "the edits are the issue's own",
                    }
                ],
            },
        )

    return launch


def test_a_second_release_on_the_block_the_reviewer_already_read_buys_a_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """The person proposes WIDGET's line and releases: one re-review. The
    reviewer files WIDGET's line as a declaration finding again, without a
    ruling, and the node is held. A second release with the body unchanged
    finds the block still answering the round, but the reviewer has read it
    at this head: one more remediation, never a second re-review."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick() -> Any:
            return _tick(
                store,
                tmp_path,
                head,
                repo_root=repo,
                launcher=_asks_for_widget_again(prompts),
                remediations=launched,
            )

        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        tick()
        assert len(prompts) == 1, "the release earns one re-review"
        assert store.latest_review_round(ITEM).round == 2
        assert tick().held == [ITEM]
        assert _held_reason(store).startswith(
            f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: "
        )

        _unhold(store, head)
        outcome = tick()
        rounds = store.latest_review_round(ITEM).round

    assert len(prompts) == 1, "no second re-review on the same block"
    assert rounds == 2
    assert [c.head_sha for c in launched] == [head, head]
    assert outcome.remediating == [ITEM]


def test_a_re_review_a_release_earned_survives_a_tick_its_reviewer_cannot_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """The person proposes WIDGET's line and releases, but the tick after the
    release has no reviewer to launch. The earned re-review is not spent on
    that deferral: no remediation runs then, and the next tick that can launch
    the reviewer re-reviews the head."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    reviewer = _asks_for_widget_again(prompts)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick(launcher: Any) -> Any:
            return _tick(
                store,
                tmp_path,
                head,
                repo_root=repo,
                launcher=launcher,
                remediations=launched,
            )

        _held_after_one_no_op(store, tmp_path, head, lambda: tick(reviewer), launched)
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head)
        deferred = tick(None)
        assert deferred.unwired == [ITEM]
        assert len(launched) == 1, "a deferred reviewer buys no remediation"

        tick(reviewer)
        rounds = store.latest_review_round(ITEM).round

    assert len(prompts) == 1, "the earned re-review runs once it can launch"
    assert rounds == 2
    assert len(launched) == 1


def test_a_release_at_the_head_a_no_op_published_over_a_moved_base_re_reviews_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """The base moved during the remediation: kew's merge published H' with
    the tree it handed the agent, and the node is held ``remediation_no_change``
    at H' while the round stands at H. The person's proposal and release earn
    H' its one re-review, and that re-review's NEEDS_CHANGES is held."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "merge the moved base")
    merged = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick(at: str) -> Any:
            return _drive_tick(
                store, tmp_path, at, repo, prompts=prompts, launched=launched
            )

        tick(head)
        assert len(launched) == 1
        _no_op_remediation(
            store, tmp_path, head, "rem-1", published=merged, tree="t" * 40
        )
        assert tick(merged).held == [ITEM]
        assert _held_reason(store).startswith(f"{REMEDIATION_NO_CHANGE_HOLD_PREFIX}: ")

        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, merged)
        tick(merged)
        assert len(launched) == 1, "re-reviewed, never remediated"
        assert len(prompts) == 1
        re_review = store.latest_review_round(ITEM)
        assert (re_review.round, re_review.head_sha) == (2, merged)

        outcome = tick(merged)

    assert outcome.held == [ITEM]
    assert len(launched) == 1


def test_a_send_back_hold_at_a_head_base_integration_moved_records_no_block(
    tmp_path: Path, forge: _Forge
) -> None:
    """A send-back stands at H2, a head base integration moved past the one
    its no-op remediation published. The hold there records no block: H2 is
    neither the round's head nor a no-op's. A release that vouches for no
    head leaves the send-back standing, and buys a remediation at H2."""
    forge.body = _proposed_body(GADGET_LINE)
    integrated = "e" * 40
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(
            store, tmp_path, HEAD, review_mode=REVIEW_MODE_OPERATOR_SEND_BACK
        )

        def tick(at: str) -> Any:
            return _drive_tick(
                store, tmp_path, at, tmp_path, prompts=prompts, launched=launched
            )

        tick(HEAD)
        _no_op_remediation(
            store, tmp_path, HEAD, "rem-1", published="c" * 40, tree="t" * 40
        )
        assert tick(integrated).held == [ITEM]
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, None)
        outcome = tick(integrated)

    assert prompts == []
    assert [c.head_sha for c in launched] == [HEAD, integrated]
    assert outcome.remediating == [ITEM]


def test_a_release_whose_head_base_integration_moved_earns_that_head_no_re_review(
    tmp_path: Path, forge: _Forge
) -> None:
    """The send-back's hold was set, and its block recorded, at H', the head
    its no-op remediation published. The person proposes and releases at H',
    then a base integration moves the PR to H2 before the next pass: the
    send-back stands again on H2, and the release, set for H', buys H2 a
    remediation rather than a re-review."""
    forge.body = _proposed_body(GADGET_LINE)
    published, integrated = "c" * 40, "e" * 40
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(
            store, tmp_path, HEAD, review_mode=REVIEW_MODE_OPERATOR_SEND_BACK
        )

        def tick(at: str) -> Any:
            return _drive_tick(
                store, tmp_path, at, tmp_path, prompts=prompts, launched=launched
            )

        tick(HEAD)
        _no_op_remediation(
            store, tmp_path, HEAD, "rem-1", published=published, tree="t" * 40
        )
        assert tick(published).held == [ITEM]
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, published)
        outcome = tick(integrated)

    assert prompts == []
    assert [c.head_sha for c in launched] == [HEAD, integrated]
    assert outcome.remediating == [ITEM]


def test_with_no_approvers_listed_any_operators_release_earns_the_re_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    """An empty ``[approval] approvers`` list is anyone, as for every other
    approval gate: the bystander's release with a proposed line re-reviews."""
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    forge.body = _proposed_body(GADGET_LINE)
    launched: list[RemediationContext] = []
    prompts: list[str] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _declaration_round(store, tmp_path, head)

        def tick() -> Any:
            return _tick(
                store,
                tmp_path,
                head,
                repo_root=repo,
                launcher=_reviewer([{**_REJECT_GADGET, "node_id": WIDGET}], prompts),
                remediations=launched,
                approvers=(),
            )

        _held_after_one_no_op(store, tmp_path, head, tick, launched)
        assert propose_lines("o/r", PR, [WIDGET_LINE])
        _unhold(store, head, actor="bystander")
        tick()

    assert len(launched) == 1
    assert len(prompts) == 1


# --- A proposal no declaration is needed for is dropped, not a finding -------

_UNNEEDED_WIDGET = {
    "node_id": WIDGET,
    "answer": "rejected",
    "ground": "not_needed",
    "reason": "the base copy passes against the head, so there is nothing to declare",
}
_UNNEEDED_GADGET = {
    "node_id": GADGET,
    "answer": "rejected",
    "ground": "not_needed",
    "reason": "additions only, so there is nothing to declare",
}
_WRONG_GADGET = {**_REJECT_GADGET, "ground": "wrong"}


def test_a_round_whose_only_rejections_are_not_needed_is_ready_and_drops_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_UNNEEDED_WIDGET, _UNNEEDED_GADGET], []),
            remediations=launched,
        )
        latest = store.latest_review_round(ITEM)
        holds = store.list_epic_holds(EPIC)

    assert outcome.held == [] and holds == []
    assert latest is not None and latest.verdict == "READY"
    assert launched == [], "nothing for a remediation to do"
    findings = parse_verdict_sidecar(Path(latest.sidecar_path).read_bytes()).findings
    assert findings == []
    assert proposed_section_lines(forge.body) == [], "the unneeded lines are dropped"
    assert "**Proposed behavior changes**" in forge.body, "the heading stays"
    assert declared_behavior_changes(forge.body) == frozenset(), "and never declared"
    assert forge.body.startswith("Closes #3457\n\n**Cause:** the widget miscounts")


def test_a_wrong_rejection_still_blocks_beside_a_not_needed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_UNNEEDED_WIDGET, _WRONG_GADGET], []),
        )
        latest = store.latest_review_round(ITEM)

    assert outcome.held == []
    assert latest is not None and latest.verdict == "NEEDS_CHANGES"
    findings = parse_verdict_sidecar(Path(latest.sidecar_path).read_bytes()).findings
    assert [f.location for f in findings] == [GADGET], "only the wrong one is a finding"
    assert proposed_section_lines(forge.body) == [GADGET_LINE], "the wrong one stays"
    assert declared_behavior_changes(forge.body) == frozenset()


def test_a_not_needed_rejection_beside_an_acceptance_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            tmp_path,
            head,
            repo_root=repo,
            launcher=_reviewer([_ACCEPT_WIDGET, _UNNEEDED_GADGET], []),
        )
        latest = store.latest_review_round(ITEM)

    assert latest is not None and latest.verdict == "READY"
    assert declared_behavior_changes(forge.body) == {WIDGET}
    assert proposed_section_lines(forge.body) == [WIDGET_LINE]


@pytest.mark.parametrize(
    "ground",
    [None, "wrong", "Not needed", "unneeded", 3],
    ids=["missing", "wrong", "misspelt", "other-word", "not-a-string"],
)
def test_a_rejection_that_does_not_say_not_needed_is_a_finding(ground: Any) -> None:
    entry: dict[str, Any] = {"node_id": GADGET, "answer": "rejected", "reason": "no"}
    if ground is not None:
        entry["ground"] = ground
    parsed = parse_verdict_sidecar(
        {"verdict": "READY", "findings": [], "declarations": [entry]}
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.location for f in parsed.findings] == [GADGET]
    assert [r.not_needed for r in parsed.declarations] == [False]


def test_a_not_needed_ruling_never_outranks_a_wrong_or_turns_an_acceptance() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "READY",
            "findings": [],
            "declarations": [
                {**_UNNEEDED_GADGET},
                {**_WRONG_GADGET},
                {**_UNNEEDED_GADGET},
                {**_ACCEPT_WIDGET, "ground": "not_needed"},
                {**_UNNEEDED_WIDGET},
            ],
        }
    )

    rulings = {r.node_id: (r.accepted, r.not_needed) for r in parsed.declarations}
    assert rulings == {GADGET: (False, False), WIDGET: (False, True)}
    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.location for f in parsed.findings] == [GADGET]


def _declared_then_proposed(*proposed: str, after: str) -> str:
    """A body declaring WIDGET, then a proposed block, then *after*."""
    return (
        "Closes #3457\n\n**Behavior changes**\n\n"
        f"- {WIDGET} — declared\n\n"
        "**Proposed behavior changes**\n\n"
        "```\n" + "".join(f"{line}\n" for line in proposed) + "```\n" + after
    )


def test_dropping_the_last_proposal_never_widens_the_behavior_changes_above_it(
    forge: _Forge,
) -> None:
    sneaky = "tests/test_sneaky.py::t"
    forge.body = _declared_then_proposed(
        f"- {GADGET} — proposed", after=f"- {sneaky} — appended after the fence\n"
    )
    before = declared_behavior_changes(forge.body)

    assert drop_proposed_lines("o/r", PR, [GADGET]) is True

    assert before == {WIDGET}
    assert declared_behavior_changes(forge.body) == {WIDGET}, "nothing new declared"
    assert proposed_section_lines(forge.body) == []
    assert "**Proposed behavior changes**" in forge.body


def test_a_drop_that_would_change_the_declarations_is_refused(
    forge: _Forge,
) -> None:
    # A hand-edited block whose own lines open a Behavior changes section and
    # end it again with a bold title; ruling that title's line "not needed"
    # would let the line after the fence join the section.
    ender = "**tests/test_z.py::t**"
    body = (
        "**Proposed behavior changes**\n\n```\n**Behaviour changes**\n"
        f"{ender}\n```\n- tests/test_sneaky.py::t — after the fence\n"
    )
    forge.body = body

    assert drop_proposed_lines("o/r", PR, [ender]) is False

    assert forge.body == body and forge.patches == 0
    assert declared_behavior_changes(forge.body) == frozenset()


def test_an_unclosed_proposed_fence_refuses_the_drop(forge: _Forge) -> None:
    body = (
        "Closes #3457\n\n**Proposed behavior changes**\n\n"
        f"```\n- {GADGET} — proposed\n\n"
        "**Behavior changes**\n\n"
        f"- {WIDGET} — accepted earlier\n"
    )
    forge.body = body

    assert drop_proposed_lines("o/r", PR, [GADGET, WIDGET]) is False

    assert forge.body == body and forge.patches == 0
    assert declared_behavior_changes(forge.body) == {WIDGET}


def test_a_needs_changes_whose_only_rejections_are_not_needed_is_unparseable() -> None:
    """What docs/how-it-works.md says: a not_needed rejection is no finding,
    so a NEEDS_CHANGES with nothing else has no evidenced finding to stand on."""
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [],
            "declarations": [_UNNEEDED_WIDGET, _UNNEEDED_GADGET],
        }
    )

    assert parsed.unparseable
    assert parsed.unparseable_reason == "NEEDS_CHANGES with no evidenced findings"


def test_a_needs_changes_whose_only_rejections_are_not_needed_holds_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_repo(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    body = forge.body

    def launch(ctx: Any) -> Any:
        sidecar = {
            "verdict": "NEEDS_CHANGES",
            "findings": [],
            "declarations": [_UNNEEDED_WIDGET, _UNNEEDED_GADGET],
        }
        return stream_verdict(ctx, sidecar)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(store, tmp_path, head, repo_root=repo, launcher=launch)

    assert outcome.held == [ITEM]
    assert forge.body == body, "an unparseable verdict drops nothing"
