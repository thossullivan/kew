"""``kew epic send-back``: an operator NEEDS_CHANGES on a READY epic node (#3070).

kew's reviewer passed the head, so the drive reads the node "in_review —
awaiting merge" and acts on nothing the operator posts on the PR. The send-back
records the operator's blockers as a round the drive's own review stage
remediates, under the node's item id, so the drive keeps owning the node.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support.driver_fakes import (
    EPIC,
    NOW,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _facts,
    _node,
)
from support.gh_fakes import (
    FakeForge,
    GhProcessBoundary,
    PlaceholderForge,
    install_placeholder_forge,
)
from support.prompt_profiles import save_launch_row
from typer.testing import CliRunner

from kew import subprocess_utils
from kew.audit_chain import verify_chain
from kew.cli import app
from kew.config import (
    DispatchConfig,
    EpicConfig,
    ProjectConfig,
    ReviewConfig,
    SandboxConfig,
)
from kew.graph.driver.merge import (
    REVIEW_SKIP_NOT_READY,
    ApprovalDecision,
    _review_block_reason,
)
from kew.graph.driver.review_launcher import sandbox_remediator
from kew.graph.driver.review_stage import RemediationContext
from kew.graph.driver.tick import advance
from kew.notifier import CompositeNotifier
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.review_checkout import ReviewCheckout
from kew.review_contract import is_host_location, parse_verdict_sidecar
from kew.review_outcome import STATUS_COMPLETED, ReviewOutcome, record_review_outcome
from kew.run_store import REVIEW_REMEDIATION_RUN_KIND, RunRecord, SqliteRunStore
from kew.service.send_back import (
    MAX_REASON_CHARS,
    SEND_BACK_EVENT,
    PullRequestHead,
    SendBackRefused,
    epic_send_back,
)
from kew.tracking import DispatchResult

HEAD = "a" * 40
#: The head after a merge-main step moved the PR past the READY one.
MOVED = "c" * 40
NODE = "1"
#: The first PR number :class:`FakeForge` hands out.
PR = 70
BRANCH = "agent/issue-1"
REASON = "Codex round 1: the retry loop never re-reads the claim binding."
APPROVER = "alice"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _placeholder_forge_for_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the ``gh`` these ticks send from the placeholder repo: a
    remediation launch reads the PR body to remember its proposed lines
    (#3475), and GitHub answers ``o/r`` with 404. A test that scripts the
    forge installs its own over this one; conftest's guard still refuses any
    launch of the operator's ``gh``."""
    install_placeholder_forge(monkeypatch)


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig(
        epic=EpicConfig(stall_days=3),
        # The default budget of two rounds: the reviewer's READY plus the
        # send-back must not exhaust it before the remediation runs.
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=2),
        project=ProjectConfig(repo="o/r"),
        # The remediation runs on the host, so no container runtime stands
        # between the drive and the agent launch faked below.
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    config.review.publish_status = False
    config.logging.dir = str(tmp_path / "logs")
    return config


def _ready(store: SqliteRunStore, *, epic_ref: str = EPIC, head: str = HEAD) -> None:
    store.record_review_round(
        epic_ref, NODE, head_sha=head, verdict="READY", review_mode="full"
    )


def _open_pr(head: str = HEAD) -> Callable[[], PullRequestHead]:
    """The forge's answer for the node's PR, open at *head*."""
    return lambda: PullRequestHead(number=PR, state="OPEN", head_sha=head)


def _send_back(
    store: SqliteRunStore,
    config: DispatchConfig,
    *,
    reason: str = REASON,
    read_pr: Callable[[], PullRequestHead] | None = None,
) -> Any:
    return epic_send_back(
        store,
        epic_ref=EPIC,
        item_id=NODE,
        reason=reason,
        config=config,
        read_pr=read_pr or _open_pr(),
    )


def _approved_pr(head: str) -> dict[str, Any]:
    """The PR as ``gh pr view`` returns it: open, CLEAN, approved after READY."""
    return {
        "number": PR,
        "state": "OPEN",
        "headRefName": BRANCH,
        "headRefOid": head,
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "commits": [],
        "author": {"login": "kew-agent"},
        "latestReviews": [
            {
                "state": "APPROVED",
                # The merge gate needs the approval to postdate the READY round.
                "submittedAt": "2099-01-01T00:00:00Z",
                "author": {"login": APPROVER, "is_bot": False, "__typename": "User"},
            }
        ],
    }


@dataclass
class _TickForge(FakeFactsReader):
    """The tick's GitHub reads and ``gh`` writes, scripted.

    *on_read* runs inside the named forge read — ``head_checks`` (after the
    merge pass's review gate, before its merge) or ``fresh_pr_facts`` (the
    merge queue's re-read once its verification is green): the windows an
    operator's send-back can land in.
    """

    gh_calls: list[list[str]] = field(default_factory=list)
    on_read: dict[str, Callable[[], object]] = field(default_factory=dict)

    def _fire(self, read: str) -> None:
        hook = self.on_read.pop(read, None)
        if hook is not None:
            hook()

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        self._fire("head_checks")
        return self.checks

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        self._fire("fresh_pr_facts")
        return self.pr_facts(pr_number)

    def run(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.gh_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def merges(self) -> list[list[str]]:
        return [c for c in self.gh_calls if c[:3] == ["gh", "pr", "merge"]]


def _tick_forge(head: str = HEAD) -> _TickForge:
    return _TickForge(
        issue_facts_by_num={1: _facts(1, pr_number=PR, pr_state="OPEN", head_oid=head)},
        pr_facts_by_num={PR: _approved_pr(head)},
        permissions_by_login={APPROVER: "write"},
    )


def _tick(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    *,
    remediator: Any,
    facts: FakeFactsReader | None = None,
    approvers: list[str] | None = None,
) -> Any:
    """One real drive tick over a node whose PR is open."""
    return advance(
        EPIC,
        config=config,
        store=store,
        deps=FakeSource(nodes=[_node(NODE)], edges=[]),
        facts=facts or _tick_forge(),  # type: ignore[arg-type]
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=approvers or [],
        repo="o/r",
        remediator=remediator,
    )


def _seam_tick(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    *,
    facts: FakeFactsReader | None = None,
) -> list[RemediationContext]:
    """A tick whose remediator seam only records the context it is handed: for
    the tests about WHETHER the drive remediates, not what the launch carries."""
    calls: list[RemediationContext] = []
    _tick(store, config, tmp_path, remediator=calls.append, facts=facts)
    return calls


def _install_forge(monkeypatch: pytest.MonkeyPatch, forge: Any) -> None:
    """Answer every ``gh`` argv kew sends from *forge*; ``git`` stays real."""
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setitem(
        subprocess_utils.TOOL_PATHS, "git", shutil.which("git") or "git"
    )
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )


def _node_forge(tmp_path: Path, head: str = HEAD) -> FakeForge:
    """Issue 1, closed by its PR, which is open at *head*."""
    forge = FakeForge(origin=tmp_path / "origin.git")
    forge.open_issue(1, epic=EPIC)
    pr = forge.open_pr(issue=1, branch=BRANCH, head=head)
    assert pr["number"] == PR
    pr["title"] = "node 1"
    forge.issues[1]["closedByPullRequestsReferences"] = [
        {"number": PR, "url": pr["url"]}
    ]
    return forge


@dataclass
class _Launches:
    """The agents the real remediation path launched, as kew handed them over."""

    forge: FakeForge
    head: str = HEAD
    calls: list[dict[str, Any]] = field(default_factory=list)

    def at(self, head: str) -> None:
        """Move the PR — and the checkout the review materializes — to *head*."""
        self.head = head
        self.forge.prs[PR]["headRefOid"] = head


@pytest.fixture
def agent_launches(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Launches]:
    """The production remediator, faked only where it leaves the process.

    ``gh`` answers from a :class:`FakeForge` at the subprocess boundary, and the
    agent CLI the runner preflight looks up is a stub on PATH. The other edges
    — the review checkout (``git worktree``), the agent launch, the push of the
    agent's commit and the notification sink — are stand-ins that record
    what kew handed them. The launch stand-in writes the run row the real run
    writer would, from the arguments kew passed it.
    """
    launches = _Launches(forge=_node_forge(tmp_path))
    _install_forge(monkeypatch, launches.forge)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent_cli = bin_dir / "claude"
    agent_cli.write_text("#!/bin/sh\nexit 0\n")
    agent_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()

    def _checkout(**_kwargs: Any) -> ReviewCheckout:
        return ReviewCheckout(checkout_dir, launches.head, BRANCH, True, None, None)

    def _launch(**kwargs: Any) -> DispatchResult:
        launches.calls.append(kwargs)
        save_launch_row(store, kwargs)
        return DispatchResult(
            exit_code=0,
            issue_num=int(kwargs["issue_num"]),
            log_file=tmp_path / "run.log",
        )

    with (
        patch("kew.review.materialize_review_checkout", side_effect=_checkout),
        patch("kew.review.run_tracked_agent", side_effect=_launch),
        patch(
            "kew.review._canonical_review_publication", return_value=("b" * 40, False)
        ),
        patch("kew.review.discard_checkout"),
        # The outcome's notification sink: no backend, so nothing is sent.
        patch("kew.review_outcome.build_notifier", return_value=CompositeNotifier([])),
    ):
        yield launches


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: False
    )


@pytest.fixture
def store(tmp_path: Path) -> Any:
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as s:
        yield s


def _remediation_runs(store: SqliteRunStore, item_id: str) -> list[Any]:
    return [
        r
        for r in store.list_by_item(item_id)
        if r.run_kind == REVIEW_REMEDIATION_RUN_KIND
    ]


def test_the_next_tick_remediates_a_sent_back_ready_node_under_its_item_id(
    tmp_path: Path, store: SqliteRunStore, agent_launches: _Launches
) -> None:
    config = _config(tmp_path)
    remediator = sandbox_remediator(config, store, epic_ref=EPIC)
    _ready(store)
    _tick(store, config, tmp_path, remediator=remediator)
    assert agent_launches.calls == []  # READY on the head: awaiting merge

    sent = _send_back(store, config)
    assert (sent.item_id, sent.head_sha, sent.round) == (NODE, HEAD, 2)

    _tick(store, config, tmp_path, remediator=remediator)

    # One agent launched, by the governed remediation, under the node's id.
    [launch] = agent_launches.calls
    assert launch["item_id"] == NODE
    assert launch["run_kind"] == REVIEW_REMEDIATION_RUN_KIND
    # The brief that agent was handed carries the operator's reason.
    assert REASON in launch["prompt"]
    assert [r.run_id for r in _remediation_runs(store, NODE)] == [launch["run_id"]]
    # Not the `kew review <PR>` path: no run and no round is keyed to the PR
    # number, and the round cap did not convert the send-back into a hold.
    assert store.list_by_item(str(PR)) == []
    assert store.latest_review_round(str(PR)) is None
    assert store.list_epic_holds(EPIC) == []
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_a_send_back_after_the_head_moved_since_ready_records_nothing(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """READY stands on HEAD; a merge-main step then moved the PR to MOVED. A
    round on MOVED would be the base of the next delta review, so HEAD..MOVED
    would merge unreviewed; a round on HEAD would read as stale. Neither is
    recorded: the operator waits for the drive to review MOVED."""
    config = _config(tmp_path)
    config.review.publish_status = True
    statuses = _StatusRecorder()
    _install_forge(monkeypatch, statuses)
    _ready(store, head=HEAD)

    with pytest.raises(
        SendBackRefused,
        match=f"head moved since its READY review \\({HEAD[:12]} → {MOVED[:12]}\\)",
    ):
        _send_back(store, config, read_pr=_open_pr(MOVED))

    _assert_nothing_recorded(store, 1)
    assert statuses.calls == []
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_a_pr_that_merged_while_the_send_back_ran_is_reported(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Open when read, merged by the time the round landed: the round stays
    recorded, and the send-back says it came too late."""
    _ready(store)
    answers = iter(
        [PullRequestHead(PR, "OPEN", HEAD), PullRequestHead(PR, "MERGED", HEAD)]
    )

    sent = _send_back(store, _config(tmp_path), read_pr=lambda: next(answers))

    assert sent.merged_meanwhile is True
    latest = store.latest_review_round(NODE)
    assert latest is not None
    assert latest.verdict == "NEEDS_CHANGES"


def test_a_forge_read_that_fails_after_the_round_landed_keeps_the_send_back(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The second read only reports a late merge: when it fails, the recorded
    round still stands and the send-back still succeeds."""
    _ready(store)
    reads = iter([PullRequestHead(PR, "OPEN", HEAD)])

    def read_pr() -> PullRequestHead:
        try:
            return next(reads)
        except StopIteration:
            raise RuntimeError("gh: HTTP 502") from None

    sent = _send_back(store, _config(tmp_path), read_pr=read_pr)

    assert sent.merged_meanwhile is False
    latest = store.latest_review_round(NODE)
    assert latest is not None
    assert latest.verdict == "NEEDS_CHANGES"


def test_a_send_back_on_a_pr_still_open_afterwards_is_not_reported_merged(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    _ready(store)

    assert _send_back(store, _config(tmp_path)).merged_meanwhile is False


class _StatusRecorder:
    """A forge that records every ``gh`` argv and answers each with success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> Any:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")


@pytest.mark.parametrize(
    ("pr", "refusal"),
    [
        pytest.param(PullRequestHead(PR, "MERGED", HEAD), "is merged", id="merged"),
        pytest.param(PullRequestHead(PR, "CLOSED", HEAD), "is closed", id="closed"),
        pytest.param(PullRequestHead(None, None, None), "no pull request", id="no-pr"),
        pytest.param(
            PullRequestHead(PR, None, None), "state could not be read", id="unread"
        ),
        pytest.param(
            PullRequestHead(PR, "OPEN", None), "head could not be read", id="no-head"
        ),
    ],
)
def test_a_send_back_on_a_pr_that_is_not_open_records_nothing(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    pr: PullRequestHead,
    refusal: str,
) -> None:
    config = _config(tmp_path)
    config.review.publish_status = True
    statuses = _StatusRecorder()
    _install_forge(monkeypatch, statuses)
    _ready(store)

    with pytest.raises(SendBackRefused, match=refusal):
        _send_back(store, config, read_pr=lambda: pr)

    _assert_nothing_recorded(store, 1)
    assert statuses.calls == []  # no NEEDS_CHANGES commit status published


@pytest.mark.parametrize("blocking", [True, False], ids=["blocking", "advisory"])
def test_a_send_back_after_a_remediated_round_still_remediates(
    tmp_path: Path, store: SqliteRunStore, blocking: bool
) -> None:
    """The common history: round 1 NEEDS_CHANGES, remediated, round 2 READY.

    Those two rounds already fill the default budget of 2; the send-back opens
    a fresh one, so the next tick remediates instead of holding the node
    ``review_rounds_exhausted`` (blocking) or attesting DEGRADED (advisory).
    """
    config = _config(tmp_path)
    config.review.blocking = blocking
    store.record_review_round(
        EPIC, NODE, head_sha="b" * 40, verdict="NEEDS_CHANGES", review_mode="full"
    )
    _ready(store)
    _send_back(store, config)

    calls = _seam_tick(store, config, tmp_path)

    assert [ctx.item_id for ctx in calls] == [NODE]
    assert store.list_epic_holds(EPIC) == []
    latest = store.latest_review_round(NODE)
    assert latest is not None
    assert latest.review_mode == "operator_send_back"


def test_the_budget_a_send_back_opens_is_still_capped(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Only the rounds before the send-back are forgiven: two reviewer rounds
    after it spend the fresh budget, and the node holds as it always would."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    for _ in range(2):
        store.record_review_round(
            EPIC, NODE, head_sha=HEAD, verdict="NEEDS_CHANGES", review_mode="full"
        )

    calls = _seam_tick(store, config, tmp_path)

    assert calls == []
    assert [h.reason for h in store.list_epic_holds(EPIC)] == [
        "review_rounds_exhausted"
    ]


def test_a_send_back_is_attested_with_the_human_and_the_reason(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    _ready(store)

    _send_back(store, _config(tmp_path), reason=f"  {REASON}\n")

    latest = store.latest_review_round(NODE)
    assert latest is not None
    assert (latest.verdict, latest.head_sha) == ("NEEDS_CHANGES", HEAD)
    assert latest.review_mode == "operator_send_back"
    events = {e.event: e for e in store.list_audit_events(item_id=NODE)}
    verdict = events[REVIEW_VERDICT_EVENT]
    assert (verdict.actor_type, verdict.actor_id) == ("human", "tom")
    assert verdict.payload["verdict"] == "NEEDS_CHANGES"
    assert verdict.payload["sidecar_digest"] == latest.sidecar_digest
    sent = events[SEND_BACK_EVENT]
    assert (sent.actor_type, sent.actor_id) == ("human", "tom")
    assert sent.payload["reason"] == REASON
    assert sent.payload["head_sha"] == HEAD
    assert verify_chain(store.read_chain_rows()).ok


def _assert_nothing_recorded(store: SqliteRunStore, rounds: int) -> None:
    assert len(store.list_review_rounds(NODE)) == rounds
    assert [
        e
        for e in store.list_audit_events(item_id=NODE)
        if e.event in {SEND_BACK_EVENT, REVIEW_VERDICT_EVENT}
    ] == []


@pytest.mark.parametrize(
    ("setup", "reason", "refusal"),
    [
        pytest.param(lambda s: None, REASON, "no review round", id="never-reviewed"),
        pytest.param(
            lambda s: _ready(s, epic_ref="epic:other"),
            REASON,
            "no review round of epic",
            id="other-epic",
        ),
        pytest.param(
            lambda s: s.record_review_round(
                EPIC, NODE, head_sha=HEAD, verdict="NEEDS_CHANGES"
            ),
            REASON,
            "not READY",
            id="already-remediating",
        ),
        pytest.param(_ready, "  \n", "empty", id="empty-reason"),
        pytest.param(
            lambda s: (_ready(s), s.hold_epic_node(EPIC, 1, "ci_pending")),
            REASON,
            "kew epic unhold",
            id="held",
        ),
        pytest.param(
            lambda s: (_ready(s), s.reserve_epic_node_item(REVIEW_EPIC_REF, NODE, "r")),
            REASON,
            "holds 1's reservation",
            id="run-in-flight",
        ),
    ],
)
def test_a_send_back_that_does_not_apply_records_nothing(
    tmp_path: Path, store: SqliteRunStore, setup: Any, reason: str, refusal: str
) -> None:
    setup(store)
    rounds = len(store.list_review_rounds(NODE))

    with pytest.raises(SendBackRefused, match=refusal):
        _send_back(store, _config(tmp_path), reason=reason)

    _assert_nothing_recorded(store, rounds)


def test_a_reason_one_character_over_the_limit_records_nothing(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    config = _config(tmp_path)
    _ready(store)

    with pytest.raises(SendBackRefused, match=f"the limit is {MAX_REASON_CHARS}"):
        _send_back(store, config, reason="x" * (MAX_REASON_CHARS + 1))
    _assert_nothing_recorded(store, 1)

    # The limit itself is accepted, and lands whole in the stored finding.
    _send_back(store, config, reason="x" * MAX_REASON_CHARS)
    latest = store.latest_review_round(NODE)
    assert latest is not None
    findings = parse_verdict_sidecar(Path(str(latest.sidecar_path)).read_bytes())
    assert "x" * MAX_REASON_CHARS in findings.findings[0].summary


@pytest.mark.parametrize(
    ("verdict", "refusal"),
    [
        pytest.param("READY", "round 2 was recorded while", id="new-ready-round"),
        pytest.param("NEEDS_CHANGES", "not READY", id="new-needs-changes-round"),
    ],
)
def test_a_round_recorded_while_the_forge_is_read_is_caught_inside_the_reservation(
    tmp_path: Path, store: SqliteRunStore, verdict: str, refusal: str
) -> None:
    """The reviewer recorded a round between the send-back's first look and its
    reservation — here, while the send-back read the PR from the forge. The
    send-back answers the round it saw, so it re-checks inside the
    reservation and refuses instead of rejecting a round it never read."""
    _ready(store)

    def _read_while_a_round_lands() -> PullRequestHead:
        store.record_review_round(
            EPIC, NODE, head_sha=HEAD, verdict=verdict, review_mode="full"
        )
        return PullRequestHead(PR, "OPEN", HEAD)

    with pytest.raises(SendBackRefused, match=refusal):
        _send_back(store, _config(tmp_path), read_pr=_read_while_a_round_lands)

    _assert_nothing_recorded(store, 2)
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


@pytest.mark.parametrize("queue", [False, True], ids=["merge-pass", "merge-queue"])
def test_a_send_back_landing_before_the_merge_stops_it_and_the_node_remediates(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    agent_launches: _Launches,
    queue: bool,
) -> None:
    """The merge pass admitted the READY node — approved, CI green and, in the
    queue's case, a green combined tree — when the operator sent it back. The
    merge must not land, and the next tick remediates the node."""
    config = _config(tmp_path)
    head = _merge_queue(config, tmp_path, monkeypatch) if queue else HEAD
    agent_launches.at(head)
    remediator = sandbox_remediator(config, store, epic_ref=EPIC)
    _ready(store, head=head)
    forge = _tick_forge(head)
    window = "fresh_pr_facts" if queue else "head_checks"
    forge.on_read[window] = lambda: _send_back(store, config, read_pr=_open_pr(head))

    first = _tick(
        store,
        config,
        tmp_path,
        remediator=remediator,
        facts=forge,
        approvers=[APPROVER],
    )

    assert window not in forge.on_read, "the send-back never ran"
    assert forge.merges() == []
    assert first.merged == ()
    blocked = store.list_audit_events(event="review_merge_blocked")
    assert [e.payload["reason"] for e in blocked] == [REVIEW_SKIP_NOT_READY]

    _tick(
        store,
        config,
        tmp_path,
        remediator=remediator,
        facts=forge,
        approvers=[APPROVER],
    )

    [launch] = agent_launches.calls
    assert launch["item_id"] == NODE
    assert REASON in launch["prompt"]
    assert forge.merges() == []


@pytest.mark.parametrize("queue", [False, True], ids=["merge-pass", "merge-queue"])
def test_without_a_send_back_the_same_tick_merges_the_ready_node(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    queue: bool,
) -> None:
    """The control for the test above: its scene does reach the merge."""
    config = _config(tmp_path)
    head = _merge_queue(config, tmp_path, monkeypatch) if queue else HEAD
    _ready(store, head=head)
    forge = _tick_forge(head)

    _tick(
        store,
        config,
        tmp_path,
        remediator=lambda ctx: None,
        facts=forge,
        approvers=[APPROVER],
    )

    assert [c[3] for c in forge.merges()] == [str(PR)]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _merge_queue(
    config: DispatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Opt the epic into the serial merge queue over a real repository.

    A bare origin with ``main`` and the node's branch one commit ahead; the
    tick runs from a clone of it, so the queue's verification really merges
    the base into the head and runs its command. Returns the branch head.
    """
    origin = tmp_path / "queue-origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "README").write_text("base\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-qb", BRANCH, cwd=work)
    (work / "feature.txt").write_text("the node's change\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "the node's change", cwd=work)
    head = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", "origin", BRANCH, cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    monkeypatch.chdir(work)
    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)
    timeout_cmd = subprocess_utils.find_timeout_cmd()
    if timeout_cmd:
        subprocess_utils._register_optional_tool(timeout_cmd)
    config.epic.merge_queue = True
    config.commands.verify_loop = "true"
    config.branches.worktree_base = str(tmp_path / "wt")
    return head


def _decision(approved_at: str) -> ApprovalDecision:
    return ApprovalDecision(
        reviewer=APPROVER,
        actor_type="human",
        permission="write",
        review={"state": "APPROVED", "submittedAt": approved_at},
    )


def test_the_merge_gate_reads_a_sent_back_round_as_not_ready(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The invariant itself: the READY round passes the gate, the send-back
    recorded on top of it at the same head does not."""
    _ready(store)
    ready = store.latest_review_round(NODE)
    assert ready is not None
    approved_at = (ready.recorded_at + timedelta(hours=1)).isoformat()
    assert (
        _review_block_reason(ready, head_oid=HEAD, decision=_decision(approved_at))
        is None
    )

    _send_back(store, _config(tmp_path))

    sent_back = store.latest_review_round(NODE)
    assert sent_back is not None
    assert (
        _review_block_reason(sent_back, head_oid=HEAD, decision=_decision(approved_at))
        == REVIEW_SKIP_NOT_READY
    )


def _send_back_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    actor: str,
    forge: FakeForge | None = None,
) -> Any:
    """``kew epic send-back`` as an operator runs it: a kew.toml in the cwd,
    and GitHub on the far side of ``gh``."""
    (tmp_path / "kew.toml").write_text(
        "[project]\n"
        'repo = "o/r"\n'
        "[logging]\n"
        f'dir = "{tmp_path / "logs"}"\n'
        "[review]\n"
        "adversarial = true\n"
        "blocking = true\n"
        "publish_status = false\n"
        "[approval]\n"
        'approvers = ["tom"]\n'
    )
    reason_file = tmp_path / "reason.md"
    reason_file.write_text(REASON)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_ACTOR", actor)
    forge = forge or _node_forge(tmp_path)
    _install_forge(monkeypatch, forge)
    with patch("kew.cli.preflight_tools"):
        result = runner.invoke(
            app, ["epic", "send-back", EPIC, NODE, "--reason-file", str(reason_file)]
        )
    assert forge.unmodelled == []
    return result


def test_cli_send_back_names_the_round_and_the_head(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)

    result = _send_back_cli(tmp_path, monkeypatch, actor="tom")

    assert result.exit_code == 0, result.output
    assert "round 2" in result.output
    assert HEAD[:12] in result.output
    latest = store.latest_review_round(NODE)
    assert latest is not None
    assert latest.verdict == "NEEDS_CHANGES"


def test_cli_send_back_refuses_a_head_that_moved_since_the_ready_review(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store, head=HEAD)

    result = _send_back_cli(
        tmp_path, monkeypatch, actor="tom", forge=_node_forge(tmp_path, head=MOVED)
    )

    assert result.exit_code == 2
    assert "head moved since its READY review" in result.output
    assert "wait for the drive to review the new head" in result.output
    _assert_nothing_recorded(store, 1)


class _MergesWhenTheRoundLands(FakeForge):
    """GitHub merges the PR the moment the send-back's round is in the ledger."""

    def __init__(self, store: SqliteRunStore, *, origin: Path) -> None:
        super().__init__(origin=origin)
        self._store = store

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        latest = self._store.latest_review_round(NODE)
        if latest is not None and latest.verdict == "NEEDS_CHANGES":
            for pr in self.prs.values():
                pr["state"] = "MERGED"
        return super().__call__(cmd, **kwargs)


def test_cli_send_back_warns_when_the_pr_merged_before_it_took_effect(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    forge = _MergesWhenTheRoundLands(store, origin=tmp_path / "origin.git")
    forge.open_issue(1, epic=EPIC)
    pr = forge.open_pr(issue=1, branch=BRANCH, head=HEAD)
    forge.issues[1]["closedByPullRequestsReferences"] = [
        {"number": pr["number"], "url": pr["url"]}
    ]

    result = _send_back_cli(tmp_path, monkeypatch, actor="tom", forge=forge)

    assert result.exit_code == 0, result.output
    assert "merged before the send-back could take effect" in result.output
    latest = store.latest_review_round(NODE)
    assert latest is not None
    assert latest.verdict == "NEEDS_CHANGES"


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_cli_send_back_refuses_a_pr_that_is_no_longer_open(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _ready(store)
    forge = _node_forge(tmp_path)
    forge.prs[PR]["state"] = state

    result = _send_back_cli(tmp_path, monkeypatch, actor="tom", forge=forge)

    assert result.exit_code == 2
    assert f"PR #{PR} is {state.lower()}" in result.output
    _assert_nothing_recorded(store, 1)


def test_cli_send_back_refuses_an_actor_outside_the_approvers(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)

    result = _send_back_cli(tmp_path, monkeypatch, actor="mallory")

    assert result.exit_code == 2
    assert "approvers" in result.output
    _assert_nothing_recorded(store, 1)


def test_a_send_back_stands_when_the_head_moves_and_is_remediated_there(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Sent back at HEAD; a merge-main push then moves the PR to MOVED. The
    operator's finding is untouched by that push, so the next tick remediates
    at MOVED instead of handing MOVED to a reviewer who could pass it."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    rounds = len(store.list_review_rounds(NODE))

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert [(ctx.item_id, ctx.head_sha) for ctx in calls] == [(NODE, MOVED)]
    # No reviewer ran: no round was recorded and no review run exists.
    assert len(store.list_review_rounds(NODE)) == rounds
    assert [
        r for r in store.list_by_item(NODE) if r.run_kind != REVIEW_REMEDIATION_RUN_KIND
    ] == []
    latest = store.latest_review_round(NODE)
    assert latest is not None and latest.review_mode == "operator_send_back"


def test_a_send_back_a_remediation_answered_is_re_reviewed_on_the_moved_head(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Once a remediation has run after the send-back, the moved head is the
    remediation's work: the reviewer reads it, and nothing remediates again."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(
        store,
        config,
        cut=HEAD,
        published=MOVED,
        integrated_tree=INTEGRATED_TREE,
        published_tree=EDITED_TREE,
    )

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []


def test_a_remediation_run_with_no_completed_outcome_does_not_answer_a_send_back(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """A run row that reads success says the agent finished, not what reached
    the branch: a publication refused because the branch moved, or an outcome
    that was never written, leaves the operator's finding where it was. The
    send-back stands on MOVED and is remediated there."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    store.save(_remediation_row(started=datetime.now(UTC) + timedelta(minutes=1)))

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert [(ctx.item_id, ctx.head_sha) for ctx in calls] == [(NODE, MOVED)]
    assert _held_reasons(store) == []


#: The tree kew's own base integration left in the remediation's checkout,
#: before the agent ran.
INTEGRATED_TREE = "1" * 40
#: A tree carrying the agent's own edit on top of that integration.
EDITED_TREE = "2" * 40


def _remediation_published(
    store: SqliteRunStore,
    config: DispatchConfig,
    *,
    cut: str,
    published: str,
    integrated_tree: str = "",
    published_tree: str = "",
) -> None:
    """A remediation that ran to success after the send-back, cut at *cut* and
    publishing *published*: its run row and its completed outcome event, with
    the integrated and published trees when given."""
    store.save(_remediation_row(started=datetime.now(UTC)))
    record_review_outcome(
        ReviewOutcome(
            pr_number=PR,
            status=STATUS_COMPLETED,
            stage="runner",
            classification="completed",
            branch=BRANCH,
            head_sha=published,
            item_id=NODE,
            cut_head_sha=cut,
            integrated_tree=integrated_tree,
            published_tree=published_tree,
        ),
        store=store,
        config=config,
        notify=False,
    )


def _held_reasons(store: SqliteRunStore) -> list[str]:
    return [hold.reason for hold in store.list_epic_holds(EPIC)]


def test_a_no_op_remediation_does_not_answer_a_send_back_on_a_moved_head(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The remediation ran at HEAD and published HEAD; a merge-main step then
    moved the PR to MOVED. The operator's finding stands on MOVED, and the
    drive holds the node rather than remediating it once per tick."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []
    [reason] = _held_reasons(store)
    assert reason.startswith("remediation_no_change: ")
    assert "1 finding stands" in reason


def test_a_no_op_remediation_does_not_answer_a_send_back_on_an_unmoved_head(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(HEAD))

    assert calls == []
    [reason] = _held_reasons(store)
    assert reason.startswith("remediation_no_change: ")


def test_a_no_op_remediation_on_a_moved_head_does_not_answer_the_send_back(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """A merge-main step moved the PR to MOVED, and the remediation then ran at
    MOVED and published MOVED: its head differs from the round's, but it
    changed nothing, so the send-back still stands."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=MOVED, published=MOVED)

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []
    [reason] = _held_reasons(store)
    assert reason.startswith("remediation_no_change: ")


def test_a_remediation_that_changed_the_head_answers_the_send_back(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The remediation was cut at HEAD, kew merged the base in before the agent
    ran, and the agent edited on top of that merge: the tree it published is
    not the one the merge left, so MOVED is its work. The reviewer reads it and
    nothing is held or remediated."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(
        store,
        config,
        cut=HEAD,
        published=MOVED,
        integrated_tree=INTEGRATED_TREE,
        published_tree=EDITED_TREE,
    )

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []
    assert _held_reasons(store) == []


def test_a_remediation_that_only_merged_the_base_does_not_answer_the_send_back(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Cut at HEAD, kew merged main in before the agent ran, and the agent
    changed nothing: the remediation published MOVED = merge(HEAD, main), whose
    tree is the one the merge left. The commit moved; the operator's finding
    did not. The send-back stands and the node is held."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(
        store,
        config,
        cut=HEAD,
        published=MOVED,
        integrated_tree=INTEGRATED_TREE,
        published_tree=INTEGRATED_TREE,
    )

    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []
    [reason] = _held_reasons(store)
    assert reason.startswith("remediation_no_change: ")
    assert "moved" not in reason, "MOVED is the head the no-op published"


def test_the_no_change_hold_says_the_head_moved_since(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The no-op published HEAD, and the PR has since moved to MOVED: the
    reason says so, rather than reading as if nothing had touched the branch."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)

    _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    [reason] = _held_reasons(store)
    assert reason.startswith("remediation_no_change: ")
    assert f"the head has moved since, to {MOVED[:12]}" in reason


def _unhold_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str = HEAD,
    forge: Any = None,
) -> Any:
    """``kew epic unhold`` as an operator runs it, GitHub behind ``gh`` with
    the PR open at *head* — or behind *forge* when one is given.

    The forge is installed for this command only. A tick before it registered
    the real ``gh`` in the tool allowlist, and a test-scoped patch over that
    entry would put the real ``gh`` back after the allowlist's own restore,
    for every later test in the worker.
    """
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{tmp_path / "logs"}"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_ACTOR", APPROVER)
    forge = forge or _node_forge(tmp_path, head=head)
    with pytest.MonkeyPatch.context() as command, patch("kew.cli.preflight_tools"):
        _install_forge(command, forge)
        result = runner.invoke(app, ["epic", "unhold", EPIC, NODE])
    assert result.exit_code == 0, result.output
    return result


def test_releasing_the_no_change_hold_answers_a_send_back_on_a_moved_head(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person pushed the fix after the no-op remediation: the release is
    their word that MOVED answers the send-back, so the reviewer reads it."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)
    assert _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED)) == []
    assert len(_held_reasons(store)) == 1

    _unhold_cli(tmp_path, monkeypatch, head=MOVED)
    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []
    assert _held_reasons(store) == []


def test_a_release_on_the_head_the_no_op_published_has_that_head_reviewed(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kew merged main into the remediation's checkout and the agent changed
    nothing, so the no-op itself published MOVED. Released at MOVED, the
    send-back is answered there: the reviewer reads MOVED, and no remediation
    runs on it."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(
        store,
        config,
        cut=HEAD,
        published=MOVED,
        integrated_tree=INTEGRATED_TREE,
        published_tree=INTEGRATED_TREE,
    )
    assert _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED)) == []
    assert len(_held_reasons(store)) == 1

    _unhold_cli(tmp_path, monkeypatch, head=MOVED)
    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED))

    assert calls == []
    assert _held_reasons(store) == []


def test_a_release_that_cannot_read_the_head_says_the_send_back_still_stands(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub does not answer, so the release records no head and answers no
    send-back. The hold is still released, and the operator is told the
    send-back stands rather than left expecting a review."""
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)
    assert _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED)) == []

    result = _unhold_cli(tmp_path, monkeypatch, forge=PlaceholderForge())

    assert _held_reasons(store) == []
    [release] = store.list_audit_events(
        limit=None, event="epic_node_unhold", item_id=NODE
    )
    assert (release.payload or {})["pr_head"] is None
    output = " ".join(result.output.split())
    assert "could not read the pull request head" in output
    assert "send-back on it still stands" in output


def test_a_release_answers_the_send_back_only_on_the_head_it_was_given(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Released while the PR stood at MOVED; a base integration then moved it
    to LATER before any reviewer read MOVED. The release vouched for MOVED, not
    for LATER, so the send-back stands there and is remediated."""
    later = "d" * 40
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)
    assert _seam_tick(store, config, tmp_path, facts=_tick_forge(MOVED)) == []

    _unhold_cli(tmp_path, monkeypatch, head=MOVED)
    [release] = store.list_audit_events(
        limit=None, event="epic_node_unhold", item_id=NODE
    )
    assert (release.payload or {})["pr_head"] == MOVED
    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(later))

    assert [(ctx.item_id, ctx.head_sha) for ctx in calls] == [(NODE, later)]


def test_releasing_the_no_change_hold_on_an_unmoved_head_buys_one_remediation(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _ready(store)
    _send_back(store, config)
    _remediation_published(store, config, cut=HEAD, published=HEAD)
    assert _seam_tick(store, config, tmp_path, facts=_tick_forge(HEAD)) == []

    _unhold_cli(tmp_path, monkeypatch)
    calls = _seam_tick(store, config, tmp_path, facts=_tick_forge(HEAD))

    assert [(ctx.item_id, ctx.head_sha) for ctx in calls] == [(NODE, HEAD)]
    assert _held_reasons(store) == []


def _remediation_row(*, started: datetime) -> RunRecord:
    """A remediation that ran to success after the send-back was recorded."""
    return RunRecord(
        run_id="remediation-1",
        issue_num=1,
        branch=BRANCH,
        status="success",
        exit_code=0,
        started_at=started,
        item_id=NODE,
        run_kind=REVIEW_REMEDIATION_RUN_KIND,
    )


def test_a_head_starting_with_twelve_digits_round_trips_the_send_back_location(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    digits = "123456789012" + "a" * 28
    _ready(store, head=digits)

    _send_back(store, _config(tmp_path), read_pr=_open_pr(digits))

    latest = store.latest_review_round(NODE)
    assert latest is not None and latest.sidecar_path is not None
    findings = parse_verdict_sidecar(Path(latest.sidecar_path).read_bytes()).findings
    [finding] = findings
    assert finding.location == f"operator-send-back@{digits}"
    # Read back as a head kew wrote, never as line 123456789012 of a file.
    assert is_host_location(finding.location)


def test_an_advisory_degraded_node_is_sendable(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Advisory review attests DEGRADED and lets the node proceed to merge, so
    it awaits merge exactly as a READY one does."""
    config = _config(tmp_path)
    config.review.blocking = False
    store.record_review_round(
        EPIC, NODE, head_sha=HEAD, verdict="DEGRADED", review_mode="full"
    )

    sent = _send_back(store, config)

    assert (sent.head_sha, sent.round) == (HEAD, 2)
    latest = store.latest_review_round(NODE)
    assert latest is not None and latest.verdict == "NEEDS_CHANGES"


def test_a_degraded_node_under_blocking_review_is_refused_naming_the_mode(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    store.record_review_round(
        EPIC, NODE, head_sha=HEAD, verdict="DEGRADED", review_mode="full"
    )

    with pytest.raises(SendBackRefused, match="DEGRADED under blocking review"):
        _send_back(store, _config(tmp_path))

    _assert_nothing_recorded(store, 1)


def test_cli_send_back_with_no_live_driver_says_so_and_exits_0(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)

    result = _send_back_cli(tmp_path, monkeypatch, actor="tom")

    assert result.exit_code == 0, result.output
    assert "no live driver" in result.output
    assert f"kew epic run {EPIC}" in result.output


def test_cli_send_back_prints_no_success_line_when_the_pr_merged_meanwhile(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    forge = _MergesWhenTheRoundLands(store, origin=tmp_path / "origin.git")
    forge.open_issue(1, epic=EPIC)
    pr = forge.open_pr(issue=1, branch=BRANCH, head=HEAD)
    forge.issues[1]["closedByPullRequestsReferences"] = [
        {"number": pr["number"], "url": pr["url"]}
    ]

    result = _send_back_cli(tmp_path, monkeypatch, actor="tom", forge=forge)

    assert result.exit_code == 0, result.output
    assert "merged before the send-back could take effect" in result.output
    assert "the drive remediates it" not in result.output
    assert "no live driver" not in result.output
