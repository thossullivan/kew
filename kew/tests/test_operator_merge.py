"""`kew epic merge`: the operator merges one READY node (#2203).

The driver's merge pass needs a verified approval from a non-author human, which
a repo whose owner authors every agent PR cannot produce. The operator performs
the merge instead — and every invariant except that approval still has to hold.
These tests are the proof the issue asks for: one test per refusal (a verdict
recorded against another head, a forge head that moved, a verify that is red or
absent, a base that moved, a held node, the queue switched off) and one success
that records the attested event.

Only the forge and the queue's verification are faked. The store is real, the
gate's own reads are real, and the merge argv is asserted verbatim.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx

from kew.audit import Actor
from kew.config import DispatchConfig
from kew.graph.driver import merge_queue as _mq
from kew.graph.driver import operator_merge as _om
from kew.graph.driver.facts import GitHubFactsReader, NodeFacts
from kew.graph.driver.merge import REVIEW_SKIP_HEAD_MOVED, REVIEW_SKIP_NOT_READY
from kew.graph.driver.operator_merge import (
    OPERATOR_MERGE_EVENT,
    OperatorMergeRefused,
    operator_merge,
)
from kew.no_change import CANDIDATE, CONFIRM, NoChangeIntent
from kew.run_store_merge_queue import SUSPECT_LANDED_MISMATCH, SUSPECT_UNVERIFIED

#: `driver_fakes.store` under a private name: the `store` fixture every test
#: below takes is the one right under it, which journals the epic's membership.
_bare_store = _fx.store

EPIC = "epic/1"
ITEM = "42"
REPO = "o/r"
BASE = "main"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
VERIFIED_BASE = "c" * 40
VERIFIED_TREE = "d" * 40
LANDED_SHA = "e" * 40
BRANCH = "kew/issue-42"
PR = 7
OPERATOR = Actor("human", "tom")
#: What `GitHubFactsReader.head_checks` returns for a head whose own CI passed.
GREEN_CHECKS = ("success", "CI green (3 checks)")


def _journal_membership(store: Any, epic_ref: str, node_ids: list[str]) -> None:
    """Journal one tick of *epic_ref* whose snapshot holds exactly *node_ids*.

    The journaled snapshot is the epic's membership record, and the only one
    that is not agent-writable — so it is what the merge's membership gate reads.
    """
    store.append_epic_tick(
        epic_ref=epic_ref,
        tick_ts="2026-09-29T00:00:00+00:00",
        snapshot={
            "epic_ref": epic_ref,
            "signal": "progressing",
            "counts": {},
            "nodes": [{"id": node_id, "status": "in_review"} for node_id in node_ids],
        },
        actions={},
        snapshot_hash=f"h-{epic_ref}-{'+'.join(node_ids)}",
    )


@pytest.fixture
def store(_bare_store: Any) -> Any:
    """The real store with ITEM journaled as a node of EPIC — the state every
    gate below runs against. A merge into an epic the node is not in refuses
    before any other gate is read; `test_a_mistyped_epic_name_refuses` is that
    path, and takes the bare store instead."""
    _journal_membership(_bare_store, EPIC, [ITEM])
    return _bare_store


def _config() -> DispatchConfig:
    """A config with the queue on. Built by hand, not loaded: the loader's model
    validator would demand `[review] adversarial` and a verify command, neither
    of which these refusal paths reach."""
    config = DispatchConfig()
    config.epic.merge_queue = True
    config.commands.verify_loop = "./gate.sh"
    return config


def _facts(
    pr_number: int | None,
    item_state: tuple[str, str | None] = ("OPEN", None),
) -> NodeFacts:
    state, state_reason = item_state
    return NodeFacts(
        issue_num=int(ITEM),
        state=state,
        state_reason=state_reason,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=None,
        review_decision=None,
        merge_state_status="CLEAN",
        head_oid=HEAD,
        pr_pushed_at=None,
        # The case the live-work gate exists for: the item is closed while its PR
        # is still OPEN, so every forge gate below passes.
        pr_state="OPEN",
    )


def _at(values: list[Any], index: int) -> Any:
    """*values*[index], with the last entry repeating forever.

    Lets one fake express a fact that CHANGES between the read before the
    verification and the re-read inside the lease.
    """
    return values[min(index, len(values) - 1)]


class _FakeReader:
    """The forge, as the four reads this gate makes.

    ``heads``, ``states``, ``item_states``, ``merge_states`` and ``checks`` are
    consumed one entry per read so a head, a PR state, the tracker item's own
    state, a mergeability or a check-run conclusion that moves *during* the
    verification can be expressed; each list's last entry repeats.
    """

    def __init__(
        self,
        *,
        pr_number: int | None = PR,
        heads: list[str | None] | None = None,
        states: list[str | None] | None = None,
        item_states: list[tuple[str, str | None]] | None = None,
        branch: str | None = BRANCH,
        merge_states: list[str | None] | None = None,
        checks: list[tuple[str, str]] | None = None,
    ) -> None:
        self.pr_number = pr_number
        self.heads = heads if heads is not None else [HEAD]
        self.states = states if states is not None else ["OPEN"]
        self.item_states = item_states if item_states is not None else [("OPEN", None)]
        self.branch = branch
        self.merge_states = merge_states if merge_states is not None else ["CLEAN"]
        self.checks = checks if checks is not None else [GREEN_CHECKS]
        self.fresh_reads = 0
        self.issue_reads = 0
        self.check_reads = 0
        self.gated_checks: list[Any] = []

    def issue_facts(self, issue_num: Any) -> NodeFacts:
        item_state = _at(self.item_states, self.issue_reads)
        self.issue_reads += 1
        return _facts(self.pr_number, item_state)

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        head = _at(self.heads, self.fresh_reads)
        merge_state = _at(self.merge_states, self.fresh_reads)
        state = _at(self.states, self.fresh_reads)
        self.fresh_reads += 1
        facts: dict[str, Any] = {"number": pr_number, "state": state}
        if head is not None:
            facts["headRefOid"] = head
        if self.branch is not None:
            facts["headRefName"] = self.branch
        if merge_state is not None:
            facts["mergeStateStatus"] = merge_state
        return facts

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        """The REAL decision, bound to this fake's PR dict: a mirrored copy of
        the CLEAN/HAS_HOOKS set here could drift away from the merge pass's."""
        return GitHubFactsReader.required_checks_green(self, base, pr)  # type: ignore[arg-type]

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        self.gated_checks.append(list(gated_checks))
        assert sha == HEAD, "the head's own checks must be read for --head"
        state = _at(self.checks, self.check_reads)
        self.check_reads += 1
        return state


def _verifier(result: _mq.MergeVerifyResult, *, on_enter: Any = None) -> Any:
    """A stand-in for the queue. A context manager, like the real one: the merge
    happens INSIDE the lease, so a plain call would not exercise that at all."""

    @contextmanager
    def _verify(*_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if on_enter is not None:
            on_enter()
        bound = result
        if result.ok and not result.base_sha:
            bound = replace(result, base_sha=VERIFIED_BASE, tree_sha=VERIFIED_TREE)
        yield bound

    calls: list[dict[str, Any]] = []
    _verify.calls = calls  # type: ignore[attr-defined]
    return _verify


def _gh(
    returncode: int = 0,
    stderr: str = "",
    *,
    bases: list[str] | None = None,
    landed_tree: str = VERIFIED_TREE,
) -> Any:
    calls: list[list[str]] = []
    git_calls: list[list[str]] = []
    base_reads = bases if bases is not None else [VERIFIED_BASE, LANDED_SHA]
    reads = 0

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal reads
        if cmd[0] == "git":
            git_calls.append(cmd)
            if "ls-remote" in cmd:
                sha = _at(base_reads, reads)
                reads += 1
                return subprocess.CompletedProcess(
                    cmd, 0, f"{sha}\trefs/heads/{BASE}\n", ""
                )
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(cmd, 0, f"{landed_tree}\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    fake_run.calls = calls  # type: ignore[attr-defined]
    fake_run.git_calls = git_calls  # type: ignore[attr-defined]
    return fake_run


def _ready(store: Any, *, head: str = HEAD, verdict: str = "READY") -> None:
    store.record_review_round(
        EPIC, ITEM, head_sha=head, verdict=verdict, review_mode="full"
    )


def _merge(
    store: Any,
    *,
    config: DispatchConfig | None = None,
    reader: Any = None,
    verify: Any = None,
    run: Any = None,
    tmp_path: Path | None = None,
    epic_ref: str = EPIC,
    item_id: str = ITEM,
) -> Any:
    return operator_merge(
        config or _config(),
        store,
        epic_ref=epic_ref,
        item_id=item_id,
        head=HEAD,
        repo=REPO,
        base=BASE,
        repo_root=tmp_path or Path("/nonexistent"),
        reader=reader or _FakeReader(),  # type: ignore[arg-type]
        actor=OPERATOR,
        provider="github",
        run=run or _gh(),
        verify=verify or _verifier(_mq.VERIFIED),
    )


def _refusal(exc_info: Any) -> str:
    return exc_info.value.reason


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_the_queue_switched_off_refuses(store: Any) -> None:
    """`[epic] merge_queue = false` verifies no combined tree, so this command
    would merge a head proven green only against itself — #2148's hole."""
    _ready(store)
    config = _config()
    config.epic.merge_queue = False
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, config=config, run=gh)

    assert _refusal(exc) == _om.QUEUE_DISABLED
    assert "merge_queue" in exc.value.detail
    assert gh.calls == []


def test_a_verdict_recorded_against_another_head_refuses(store: Any) -> None:
    """READY for OTHER_HEAD reviewed other code; --head is not covered by it."""
    _ready(store, head=OTHER_HEAD)
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == REVIEW_SKIP_HEAD_MOVED
    assert OTHER_HEAD[:12] in exc.value.detail
    assert gh.calls == []


@pytest.mark.parametrize("verdict", ["NEEDS_CHANGES", "UNPARSEABLE"])
def test_a_verdict_that_is_not_ready_refuses(store: Any, verdict: str) -> None:
    _ready(store, verdict=verdict)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store)

    assert _refusal(exc) == REVIEW_SKIP_NOT_READY
    assert verdict in exc.value.detail


def test_no_review_round_at_all_refuses(store: Any) -> None:
    """No verdict is not an implicit pass — there is no grandfather here."""
    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store)

    assert _refusal(exc) == REVIEW_SKIP_NOT_READY
    assert "no review round" in exc.value.detail


def test_a_forge_head_that_moved_refuses(store: Any) -> None:
    _ready(store)
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(heads=[OTHER_HEAD]), run=gh)

    assert _refusal(exc) == _om.FORGE_HEAD_MOVED
    assert gh.calls == []


def test_an_unreadable_forge_head_refuses(store: Any) -> None:
    """A head kew cannot read is a head kew cannot bind — never "unchanged"."""
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(heads=[None]))

    assert _refusal(exc) == _om.FORGE_HEAD_UNREADABLE


def test_a_head_that_moves_during_the_verification_refuses(store: Any) -> None:
    """One verification can run for half an hour. The re-read inside the lease
    is what keeps a head nobody verified from being merged."""
    _ready(store)
    reader = _FakeReader(heads=[HEAD, OTHER_HEAD])
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=reader, run=gh)

    assert _refusal(exc) == _om.FORGE_HEAD_MOVED
    assert reader.fresh_reads == 2
    assert gh.calls == []


def test_main_moving_after_verification_refuses_before_the_merge(store: Any) -> None:
    _ready(store)
    gh = _gh(bases=[OTHER_HEAD])

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == _om.QUEUE_REFUSED
    assert _mq.MERGE_VERIFY_BASE_MOVED in exc.value.detail
    assert gh.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("skip", f"{_mq.MERGE_VERIFY_FAILED}: round 3 stands against this head"),
        ("held", f"{_mq.MERGE_VERIFY_BASE_MOVED}: main moved to cafe0000"),
        ("held", f"{_mq.MERGE_VERIFY_UNRUNNABLE}: ./gate.sh: not found"),
        ("held", f"{_mq.MERGE_QUEUE_BUSY}: merge-queue.lock is held"),
        ("held", f"{_mq.MERGE_GATE_TOUCHED}@{HEAD}: scripts/verify.sh"),
    ],
)
def test_every_queue_refusal_withholds_the_merge(
    store: Any, action: str, reason: str
) -> None:
    """A red tree, a base that moved underneath it, a gate the PR itself edits,
    a busy queue — each is the queue's own refusal, carried through verbatim."""
    _ready(store)
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            verify=_verifier(_mq.MergeVerifyResult(False, action, reason)),  # type: ignore[arg-type]
            run=gh,
        )

    assert _refusal(exc) == _om.QUEUE_REFUSED
    assert reason in exc.value.detail
    assert gh.calls == []


def test_a_missing_verify_command_refuses_through_the_real_queue(
    store: Any, tmp_path: Path
) -> None:
    """Not a stub: the REAL `verify_combined_tree` refuses an unconfigured gate
    before it clones anything, and this command carries that refusal."""
    _ready(store)
    config = _config()
    config.commands.verify_loop = ""
    config.logging.dir = str(tmp_path / "logs")
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, config=config, run=gh, verify=_mq.verify_combined_tree)

    assert _refusal(exc) == _om.QUEUE_REFUSED
    assert _mq.MERGE_VERIFY_UNCONFIGURED in exc.value.detail
    assert gh.calls == []


def test_a_held_node_refuses(store: Any) -> None:
    """A hold is a refusal the driver already recorded; `kew epic unhold` owns
    releasing it, and an operator merge must not step over it."""
    _ready(store)
    store.hold_epic_node_item(EPIC, ITEM, "ci_pending")
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == _om.NODE_HELD
    assert "ci_pending" in exc.value.detail
    assert "kew epic unhold" in exc.value.detail
    assert gh.calls == []


def test_a_hold_written_during_the_verification_refuses(store: Any) -> None:
    """One verification can run for half an hour, and the driver's sticky
    `ci_pending` hold is written on a path that never takes the queue lock — so a
    concurrent tick can hold this node WHILE the tree is being verified. Read
    only at the start, the merge would land on a node the driver just refused."""
    _ready(store)
    gh = _gh()

    def _concurrent_tick_holds() -> None:
        store.hold_epic_node_item(EPIC, ITEM, "ci_pending")

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            run=gh,
            verify=_verifier(_mq.VERIFIED, on_enter=_concurrent_tick_holds),
        )

    assert _refusal(exc) == _om.NODE_HELD
    assert "ci_pending" in exc.value.detail
    assert gh.calls == [], "merged a node the driver held mid-verification"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_needs_changes_round_recorded_during_the_verification_refuses(
    store: Any,
) -> None:
    """The review stage records rounds on a path that never takes the queue
    lock, exactly like the driver's sticky hold — so a concurrent tick can land
    a NEEDS_CHANGES round against this very head WHILE the tree is verified.
    Read only at the start, the merge would run on a verdict that no longer
    stands and attest the superseded READY round as its authority."""
    _ready(store)
    gh = _gh()

    def _concurrent_review_round() -> None:
        _ready(store, verdict="NEEDS_CHANGES")

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            run=gh,
            verify=_verifier(_mq.VERIFIED, on_enter=_concurrent_review_round),
        )

    assert _refusal(exc) == REVIEW_SKIP_NOT_READY
    assert "NEEDS_CHANGES" in exc.value.detail
    assert gh.calls == [], "merged against a verdict the review stage withdrew"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_round_rerecorded_against_another_head_mid_verification_refuses(
    store: Any,
) -> None:
    """The other half of the verdict re-proof: a READY round recorded under the
    verification against a DIFFERENT head. The forge head is still --head, so
    only the verdict's own head binding catches it."""
    _ready(store)
    gh = _gh()

    def _concurrent_review_round() -> None:
        _ready(store, head=OTHER_HEAD)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            run=gh,
            verify=_verifier(_mq.VERIFIED, on_enter=_concurrent_review_round),
        )

    assert _refusal(exc) == REVIEW_SKIP_HEAD_MOVED
    assert gh.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_the_attested_round_is_the_one_re_proven_inside_the_lease(store: Any) -> None:
    """A second READY round landing under the half-hour verification is the
    verdict this merge actually runs on, so it is the round the chain names —
    never the one read before the lease was taken."""
    first = store.record_review_round(
        EPIC, ITEM, head_sha=HEAD, verdict="READY", review_mode="full"
    )
    gh = _gh()
    rounds: list[int] = []

    def _concurrent_review_round() -> None:
        rounds.append(
            store.record_review_round(
                EPIC, ITEM, head_sha=HEAD, verdict="READY", review_mode="full"
            )
        )

    _merge(
        store,
        run=gh,
        verify=_verifier(_mq.VERIFIED, on_enter=_concurrent_review_round),
    )

    (reproven,) = rounds
    assert reproven != first, "the concurrent round must be a later one"
    (event,) = store.list_audit_events(event=OPERATOR_MERGE_EVENT)
    assert event.payload["review_round"] == reproven


# --------------------------------------------------------------------------
# The CI gates (#2033) — the operator replaces the approval, not the tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "merge_state", ["BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "UNKNOWN", None]
)
def test_a_red_required_check_refuses(store: Any, merge_state: str | None) -> None:
    """The first of the merge pass's two CI gates: GitHub's own computed
    `mergeStateStatus`. Red, pending, a stale base or unreadable — none of them
    is an approval the operator's authority stands in for."""
    _ready(store)
    gh = _gh()
    verify = _verifier(_mq.VERIFIED)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            reader=_FakeReader(merge_states=[merge_state]),
            run=gh,
            verify=verify,
        )

    assert _refusal(exc) == _om.REQUIRED_CHECKS_NOT_GREEN
    assert (merge_state or "unreadable") in exc.value.detail
    assert gh.calls == []
    # Refused before the queue was even asked: no clone, no half-hour verify.
    assert verify.calls == []


@pytest.mark.parametrize(
    "checks",
    [
        ("awaiting_approval", "awaiting ci-approved: tests did not run (test skipped)"),
        ("failure", "CI failed: test"),
        ("pending", "2/5 checks still running"),
        ("pending", "check runs unreadable: gh failed"),
        ("no_checks", "no check run concluded success"),
        ("no_evidence", "no CI evidence on this head"),
    ],
)
def test_a_head_whose_own_checks_are_not_green_refuses(
    store: Any, checks: tuple[str, str]
) -> None:
    """The #2033 gate. `mergeStateStatus` counts a SKIPPED required check as
    passing, so a private-repo agent PR whose self-hosted tests never ran reads
    CLEAN while nothing tested it — the exact case this repo is in. Pending and
    unreadable are not green either: they are "not proven", and a merge needs
    proof."""
    _ready(store)
    gh = _gh()
    verify = _verifier(_mq.VERIFIED)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(checks=[checks]), run=gh, verify=verify)

    assert _refusal(exc) == _om.HEAD_CHECKS_NOT_GREEN
    assert checks[0] in exc.value.detail
    assert checks[1] in exc.value.detail
    assert gh.calls == []
    assert verify.calls == []


def test_the_head_checks_read_carries_the_configured_gated_checks(store: Any) -> None:
    """`ci.approval_gated_checks` is what makes a skipped job an "awaiting a
    human" state rather than a pass — the same config the driver's gate reads.

    The configured names are deliberately NOT the field's default: asserted
    against the default this test would pass just as happily against a gate
    that hardcoded the two names and read no config at all."""
    _ready(store)
    config = _config()
    gated = ["operator-gate", "sbom (3.13)"]
    assert gated != DispatchConfig().ci.approval_gated_checks, (
        "the configured names must differ from the default, or this test "
        "cannot tell a gate that reads the config from one that ignores it"
    )
    config.ci.approval_gated_checks = gated
    reader = _FakeReader()

    _merge(store, config=config, reader=reader)

    assert reader.gated_checks == [gated, gated]


def test_required_checks_that_go_red_during_the_verification_refuse(
    store: Any,
) -> None:
    """A push or a re-run can turn the PR red under a half-hour verification, so
    both CI gates are re-read inside the lease, not only the head."""
    _ready(store)
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(merge_states=["CLEAN", "BLOCKED"]), run=gh)

    assert _refusal(exc) == _om.REQUIRED_CHECKS_NOT_GREEN
    assert gh.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_head_checks_that_go_red_during_the_verification_refuse(store: Any) -> None:
    _ready(store)
    gh = _gh()
    reader = _FakeReader(checks=[GREEN_CHECKS, ("failure", "CI failed: test")])

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=reader, run=gh)

    assert _refusal(exc) == _om.HEAD_CHECKS_NOT_GREEN
    assert reader.check_reads == 2
    assert gh.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


# --------------------------------------------------------------------------
# The node is still live work (#2203 review)
# --------------------------------------------------------------------------


def _no_change_judgment(store: Any, *, item_id: str = ITEM) -> Any:
    """One live zero-diff judgment for *item_id*, as a dispatch records it.

    Fresh out of `open_no_change_judgment` it is `candidate`: the run is over,
    but what its empty diff MEANT is still open — the overlay's
    AWAITING_NO_CHANGE (I10B), and nothing a merge may run against.
    """
    return store.open_no_change_judgment(
        intent=NoChangeIntent(
            source_kind="github",
            source_id=item_id,
            run_id="run-1",
            attempt_number=1,
            base_sha="c" * 40,
        ),
        policy=CONFIRM,
        now=datetime(2026, 9, 29, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("item_state", "reads"),
    [
        (("CLOSED", "NOT_PLANNED"), "cancelled"),
        (("CLOSED", "COMPLETED"), "done"),
    ],
)
def test_a_closed_tracker_item_refuses(
    store: Any, item_state: tuple[str, str | None], reads: str
) -> None:
    """A CLOSED item is a node the driver's merge pass NEVER classifies — its
    candidates are only the IN_REVIEW/HELD ones, and the overlay reads a closed
    item as DONE or CANCELLED. Nothing else here notices: an item a human
    cancelled keeps its PR open, green and READY-reviewed, so every other gate
    passes and the merge would land work the tracker says was abandoned."""
    _ready(store)
    gh = _gh()
    verify = _verifier(_mq.VERIFIED)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            reader=_FakeReader(item_states=[item_state]),
            run=gh,
            verify=verify,
        )

    assert _refusal(exc) == _om.NODE_NOT_LIVE
    assert reads in exc.value.detail
    assert gh.calls == [], "merged a PR whose tracker item is closed"
    assert verify.calls == [], "asked the queue to verify a closed item's tree"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_live_no_change_judgment_refuses(store: Any) -> None:
    """AWAITING_NO_CHANGE sits AHEAD of provider closure in the overlay (I10B):
    a zero-diff exit whose meaning has not settled is neither done nor work to
    merge, and the driver's pass never considers it either."""
    _ready(store)
    _no_change_judgment(store)
    gh = _gh()
    verify = _verifier(_mq.VERIFIED)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh, verify=verify)

    assert _refusal(exc) == _om.NODE_NOT_LIVE
    assert "awaiting_no_change" in exc.value.detail
    assert CANDIDATE in exc.value.detail
    assert gh.calls == []
    assert verify.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_an_item_closed_during_the_verification_refuses(store: Any) -> None:
    """The same fact, moved by another actor under the half-hour verification: a
    human can cancel the item while the tree is verified, and a cancelled item's
    PR stays open with an unchanged head, so nothing else re-read inside the
    lease can see it."""
    _ready(store)
    gh = _gh()
    reader = _FakeReader(item_states=[("OPEN", None), ("CLOSED", "NOT_PLANNED")])

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=reader, run=gh)

    assert _refusal(exc) == _om.NODE_NOT_LIVE
    assert reader.issue_reads == 2, "the item state must be re-read in the lease"
    assert gh.calls == [], "merged an item cancelled mid-verification"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_no_change_judgment_opened_during_the_verification_refuses(
    store: Any,
) -> None:
    """The judgment is written on a path that never takes the queue lock, like
    the hold and the review round."""
    _ready(store)
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            run=gh,
            verify=_verifier(_mq.VERIFIED, on_enter=lambda: _no_change_judgment(store)),
        )

    assert _refusal(exc) == _om.NODE_NOT_LIVE
    assert gh.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_settled_no_change_judgment_does_not_block_the_merge(store: Any) -> None:
    """A REJECTED judgment is settled: the absence of a diff was not accepted, so
    the node is live work again and its PR is the real one to merge. Only a LIVE
    judgment refuses — the overlay stops projecting a terminal one."""
    _ready(store)
    judgment = _no_change_judgment(store)
    store.reject_no_change(
        judgment_id=judgment.judgment_id,
        actor_id="tom",
        reason="the absence was not the outcome",
        now=datetime(2026, 9, 29, 1, tzinfo=UTC),
    )
    gh = _gh()

    outcome = _merge(store, run=gh)

    assert outcome.head == HEAD
    assert len(gh.calls) == 1


def test_an_unreadable_tracker_item_refuses(store: Any) -> None:
    """An item kew could not READ is not an item kew may merge. Both readers
    fold a failed item read into the empty state (a non-zero `gh issue view`,
    an unresolvable Linear workflow-state), and `settled_status` reads that as
    LIVE WORK — so without this refusal a tracker outage reads exactly like a
    healthy OPEN item, and the unreadable-refuses discipline every other gate
    here states would hold everywhere except the one gate reading the tracker."""
    from kew.graph.driver.overlay import settled_status

    assert settled_status(_facts(PR, ("", None))) is None, (
        "the precedence reads an unreadable item as live work — which is "
        "exactly why this gate has to refuse the emptiness itself"
    )
    _ready(store)
    gh = _gh()
    verify = _verifier(_mq.VERIFIED)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(
            store,
            reader=_FakeReader(item_states=[("", None)]),
            run=gh,
            verify=verify,
        )

    assert _refusal(exc) == _om.NODE_STATE_UNREADABLE
    assert "unreadable" in exc.value.detail
    assert gh.calls == [], "merged a node whose tracker item could not be read"
    assert verify.calls == [], "verified a tree for an unreadable tracker item"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_an_item_unreadable_inside_the_lease_refuses(store: Any) -> None:
    """The reviewer's case on PR #2218: the item is OPEN when the verification
    starts and the in-lease re-read FAILS. Read as live work, that read would
    merge and attest an item a human cancelled under the verification — from
    here the cancellation and the failed read are indistinguishable, so the
    failed read refuses."""
    _ready(store)
    gh = _gh()
    reader = _FakeReader(item_states=[("OPEN", None), ("", None)])

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=reader, run=gh)

    assert _refusal(exc) == _om.NODE_STATE_UNREADABLE
    assert reader.issue_reads == 2, "the item state must be re-read in the lease"
    assert gh.calls == [], "merged an item whose in-lease re-read failed"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_the_live_work_gate_reads_the_overlay_precedence(store: Any) -> None:
    """The gate derives "live work" from `overlay.settled_status` — the same
    function the tick derives node status from. Mirrored here instead, the two
    could drift, and this gate exists precisely because they must not."""
    from kew.graph.driver.overlay import settled_status

    assert (
        settled_status(_facts(PR, ("CLOSED", "NOT_PLANNED")))
        is _om.DriverStatus.CANCELLED
    )
    assert settled_status(_facts(PR, ("OPEN", None))) is None
    assert _om.DriverStatus.CANCELLED in _om._NOT_LIVE_STATUSES
    assert _om.DriverStatus.DONE in _om._NOT_LIVE_STATUSES
    assert _om.DriverStatus.AWAITING_NO_CHANGE in _om._NOT_LIVE_STATUSES
    # HELD is settled too and deliberately NOT here: `_hold_gate` and the
    # PR-open gate own those refusals, and both name a sharper reason.
    assert _om.DriverStatus.HELD not in _om._NOT_LIVE_STATUSES


# --------------------------------------------------------------------------
# Epic membership
# --------------------------------------------------------------------------


def test_a_mistyped_epic_name_refuses(_bare_store: Any) -> None:
    """Holds are keyed on the epic string exactly as typed, so under a name no
    driver ever wrote the hold gate passes VACUOUSLY — and the attested event
    would name an epic the node was never in. `kew epic unhold` refuses a wrong
    epic name; so does this."""
    store = _bare_store
    _journal_membership(store, EPIC, [ITEM])
    _ready(store)
    store.hold_epic_node_item(EPIC, ITEM, "ci_pending")
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh, epic_ref="epic/l")

    assert _refusal(exc) == _om.NOT_AN_EPIC_MEMBER
    assert "epic/l" in exc.value.detail
    assert gh.calls == []
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_an_item_that_is_not_a_node_of_the_epic_refuses(_bare_store: Any) -> None:
    """The right epic, the wrong node. Its own review round can be READY — the
    verdict table is keyed on the item, not the epic — so nothing else refuses."""
    store = _bare_store
    _journal_membership(store, EPIC, ["41"])
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store)

    assert _refusal(exc) == _om.NOT_AN_EPIC_MEMBER
    assert "41" in exc.value.detail, "the detail names the epic's real nodes"


def test_membership_is_the_shared_journaled_membership_reader(
    _bare_store: Any,
) -> None:
    """One reader, not two: the gate's membership IS
    `kew.service.epics.epic_member_ids`, so a snapshot shape the shared reader
    handles can never be read differently here (#2203 review).

    The snapshot below is the awkward one — a non-dict entry, an entry with no
    id — which a second hand-rolled copy of the parse is exactly where the two
    would drift apart."""
    from kew.service.epics import epic_member_ids

    store = _bare_store
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts="2026-09-29T00:00:00+00:00",
        snapshot={
            "epic_ref": EPIC,
            "signal": "progressing",
            "counts": {},
            "nodes": [{"id": ITEM}, {"status": "todo"}, "41", None],
        },
        actions={},
        snapshot_hash="h-odd",
    )
    _ready(store)

    assert epic_member_ids(store, EPIC) == {ITEM}
    # The member the shared reader names merges; the ids it drops refuse.
    assert _merge(store, run=_gh()).head == HEAD
    for missing in ("41", ""):
        with pytest.raises(OperatorMergeRefused) as exc:
            _merge(store, item_id=missing)
        assert _refusal(exc) == _om.NOT_AN_EPIC_MEMBER


def test_an_epic_with_no_journaled_tick_refuses(_bare_store: Any) -> None:
    """No snapshot, no membership record — and inventing one would be exactly
    the mistyped-name hole. Run `kew epic advance` first."""
    _ready(_bare_store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(_bare_store)

    assert _refusal(exc) == _om.NOT_AN_EPIC_MEMBER
    assert "journaled" in exc.value.detail


def test_a_node_with_no_pr_refuses(store: Any) -> None:
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(pr_number=None))

    assert _refusal(exc) == _om.NO_PR


def test_a_pr_that_is_not_open_refuses(store: Any) -> None:
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(states=["MERGED"]))

    assert _refusal(exc) == _om.PR_NOT_OPEN


@pytest.mark.parametrize("gone", ["MERGED", "CLOSED", None])
def test_a_pr_that_stops_being_open_during_the_verification_refuses(
    store: Any, gone: str | None
) -> None:
    """An actor outside this session can merge or close the PR while the tree is
    being verified, and a merged PR's head oid is UNCHANGED — so the head binding
    cannot see it. The state is re-read inside the lease like every other fact
    another actor can move, or `gh pr merge` would be issued for a PR that is no
    longer there. An unreadable state refuses too; it is never read as OPEN."""
    _ready(store)
    reader = _FakeReader(states=["OPEN", gone])
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=reader, run=gh)

    assert _refusal(exc) == _om.PR_NOT_OPEN
    assert reader.fresh_reads == 2, "the PR state must be re-read inside the lease"
    assert gh.calls == [], "merged a PR another actor had already closed"
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_pr_without_a_branch_name_refuses(store: Any) -> None:
    """No branch, no tree to verify — fail closed rather than merge unverified."""
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, reader=_FakeReader(branch=None))

    assert _refusal(exc) == _om.NO_BRANCH


@pytest.mark.parametrize("head", ["a" * 39, "deadbeef", "", "z" * 40])
def test_an_abbreviated_or_bogus_head_refuses(store: Any, head: str) -> None:
    """--head is compared against three full object names; an abbreviation would
    compare unequal to all of them, and `--match-head-commit` needs the full."""
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        operator_merge(
            _config(),
            store,
            epic_ref=EPIC,
            item_id=ITEM,
            head=head,
            repo=REPO,
            base=BASE,
            repo_root=Path("/nonexistent"),
            reader=_FakeReader(),  # type: ignore[arg-type]
            actor=OPERATOR,
            provider="github",
            run=_gh(),
            verify=_verifier(_mq.VERIFIED),
        )

    assert _refusal(exc) == _om.HEAD_NOT_A_SHA


def test_a_failed_merge_command_refuses_and_does_not_attest(store: Any) -> None:
    _ready(store)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=_gh(returncode=1, stderr="head commit did not match"))

    assert _refusal(exc) == _om.MERGE_FAILED
    assert "head commit did not match" in exc.value.detail
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_merge_that_lands_an_unverified_tree_is_flagged_and_not_attested(
    store: Any,
) -> None:
    _ready(store)
    gh = _gh(landed_tree="f" * 40)

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == _om.LANDED_TREE_MISMATCH
    assert len(gh.calls) == 1
    # Held on the landed commit itself: the queue waits for main to move
    # rather than verifying that unverified tree green and resuming.
    assert store.merge_queue_suspect(REPO) == (
        f"PR #{PR} landed outside its verified tree",
        LANDED_SHA,
    )
    [suspect] = store.list_merge_queue_suspects()
    assert suspect.kind == SUSPECT_LANDED_MISMATCH
    assert suspect.blocked_on == LANDED_SHA
    assert suspect.failure is not None and "not verified tree" in suspect.failure
    assert store.list_audit_events(event="merge_queue_attention")
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


def test_a_merge_whose_landed_tree_cannot_be_read_keeps_why(store: Any) -> None:
    _ready(store)
    gh = _gh(bases=[VERIFIED_BASE, "not-a-sha"])

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == _om.LANDED_TREE_MISMATCH
    [suspect] = store.list_merge_queue_suspects()
    assert suspect.kind == SUSPECT_UNVERIFIED
    assert suspect.blocked_on is None
    assert suspect.failure is not None
    assert "cannot read the tree" in suspect.failure
    assert store.list_audit_events(event=OPERATOR_MERGE_EVENT) == []


# --------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------


def test_a_ready_node_merges_and_records_the_attested_event(store: Any) -> None:
    _ready(store)
    gh = _gh()
    verify = _verifier(_mq.VERIFIED)
    reader = _FakeReader()

    outcome = _merge(store, run=gh, verify=verify, reader=reader)

    # Every forge fact was read twice: once before the verification and once
    # inside the lease, because a verification can run for half an hour.
    assert (reader.fresh_reads, reader.check_reads) == (2, 2)
    assert outcome.pr_number == PR
    assert outcome.head == HEAD
    assert outcome.base == BASE
    assert outcome.actor == "tom"
    # The fixed argv: a squash bound to the head, never --admin or --force.
    assert gh.calls == [
        [
            "gh",
            "pr",
            "merge",
            str(PR),
            "--repo",
            REPO,
            "--squash",
            "--match-head-commit",
            HEAD,
        ]
    ]
    events = store.list_audit_events(event=OPERATOR_MERGE_EVENT)
    assert len(events) == 1
    assert events[0].actor_type == "human"
    assert events[0].actor_id == "tom"
    payload = events[0].payload
    assert payload["actor"] == "tom"
    assert payload["sha"] == HEAD
    assert payload["base"] == BASE
    assert payload["pr"] == PR
    assert payload["epic"] == EPIC
    assert payload["trigger"] == "operator"
    assert payload["review_round"] == store.latest_review_round(ITEM).round
    # The queue was asked about exactly this head, base and branch.
    assert verify.calls[0]["head_oid"] == HEAD
    assert verify.calls[0]["base"] == BASE
    assert verify.calls[0]["branch"] == BRANCH
    assert verify.calls[0]["pr_number"] == PR


def test_the_merge_happens_inside_the_queue_lease(store: Any) -> None:
    """Released at the verdict instead, a second merge could land on a base the
    verified tree did not contain — the whole point of holding the lock."""
    _ready(store)
    gh = _gh()
    inside: list[int] = []

    @contextmanager
    def _leased(*_args: Any, **_kwargs: Any) -> Any:
        try:
            yield replace(
                _mq.VERIFIED,
                base_sha=VERIFIED_BASE,
                tree_sha=VERIFIED_TREE,
            )
        finally:
            inside.append(len(gh.calls))

    _merge(store, run=gh, verify=_leased)

    assert inside == [1], "the merge ran after the lease closed"


def test_an_accepted_needs_changes_round_merges(store: Any) -> None:
    """The stop rule's attested acceptance of the latest NEEDS_CHANGES round
    reads as READY (#2112) — and only when BOTH of its records agree (#2151)."""
    from kew.review_residuals import record_acceptance

    round_num = store.record_review_round(
        EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", review_mode="full"
    )
    record_acceptance(
        store,
        item_id=ITEM,
        repo=REPO,
        pr_number=PR,
        round_num=round_num,
        head_sha=HEAD,
        sidecar_digest=None,
        issue=99,
        after_rounds=round_num,
        findings=[],
    )

    # Attested but not yet MARKED: a half-written acceptance is refused (#2151).
    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store)
    assert _refusal(exc) == REVIEW_SKIP_NOT_READY

    accepted = store.accept_review_round(
        ITEM,
        round_num=round_num,
        head_sha=HEAD,
        sidecar_digest=None,
        verdict="NEEDS_CHANGES",
    )
    assert accepted

    gh = _gh()
    outcome = _merge(store, run=gh)

    assert outcome.head == HEAD
    assert len(gh.calls) == 1


def test_no_label_is_ever_read_or_written(store: Any) -> None:
    """The gate reads the round table and the forge's own heads. Not a label —
    an agent can set a label, and nothing an agent can set may merge a PR."""
    _ready(store)
    gh = _gh()

    _merge(store, run=gh)

    assert [c[:3] for c in gh.calls] == [["gh", "pr", "merge"]]


def test_the_default_verifier_is_the_real_merge_queue() -> None:
    """The wiring the refusal tests stub out: nothing else verifies the tree."""
    import inspect

    default = inspect.signature(operator_merge).parameters["verify"].default
    assert default is _mq.verify_combined_tree


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


class TestEpicMergeCli:
    """`kew epic merge` wires the gate to the real store, forge and queue."""

    def _config(self, tmp_path: Path) -> DispatchConfig:
        config = _config()
        config.project.repo = REPO
        config.logging.dir = str(tmp_path / "logs")
        return config

    def _invoke(self, tmp_path: Path, monkeypatch: Any, **kwargs: Any) -> Any:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from kew.cli import epic_app

        config = self._config(tmp_path)
        monkeypatch.setenv("KEW_ACTOR", "tom")
        with (
            patch("kew.cli.epic._c._epic_driver_config") as driver_config,
            patch("kew.cli.epic._epic_driver_sources") as sources,
            patch("kew.cli.epic._git_repo_root", return_value=tmp_path),
            patch("kew.cli.epic._c.load_config", return_value=config),
            patch("kew.cli.epic.operator_merge", **kwargs) as merge,
        ):
            driver_config.return_value = (config, REPO, 1, [])
            sources.return_value = (None, object(), None)
            result = CliRunner().invoke(epic_app, ["merge", EPIC, ITEM, "--head", HEAD])
        return result, merge

    def test_a_merge_reports_the_pr_head_and_base(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        outcome = _om.OperatorMergeOutcome(
            pr_number=PR, head=HEAD, base=BASE, branch=BRANCH, actor="tom"
        )
        result, merge = self._invoke(tmp_path, monkeypatch, return_value=outcome)

        assert result.exit_code == 0, result.output
        assert f"#{PR}" in result.output
        assert HEAD[:12] in result.output
        assert BASE in result.output
        call = merge.call_args.kwargs
        assert call["epic_ref"] == EPIC
        assert call["item_id"] == ITEM
        assert call["head"] == HEAD
        assert call["base"] == BASE
        assert call["repo"] == REPO
        # The int issue number is the key the GitHub facts reader is keyed on.
        assert call["provider"] == "github"
        assert call["actor"] == Actor("human", "tom")

    def test_a_refusal_exits_2_and_names_the_reason(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        refusal = OperatorMergeRefused(_om.NODE_HELD, "the node is held (ci_pending)")
        result, _merge = self._invoke(tmp_path, monkeypatch, side_effect=refusal)

        assert result.exit_code == 2, result.output
        assert _om.NODE_HELD in result.output
        assert "ci_pending" in result.output

    def test_a_non_item_id_exits_2_before_any_store_work(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from kew.cli import epic_app

        config = self._config(tmp_path)
        with (
            patch("kew.cli.epic._c._epic_driver_config") as driver_config,
            patch("kew.cli.epic.operator_merge") as merge,
        ):
            driver_config.return_value = (config, REPO, 1, [])
            result = CliRunner().invoke(
                epic_app, ["merge", EPIC, "not an id", "--head", HEAD]
            )

        assert result.exit_code == 2, result.output
        assert "valid github item id" in result.output
        merge.assert_not_called()


# --------------------------------------------------------------------------
# The one merge verdict rule (#3196), applied by the drive's merge step
# --------------------------------------------------------------------------


def _attest_verdict(store: Any, verdict: str, **extra: Any) -> None:
    from kew.config import ReviewConfig
    from kew.review_audit import emit_review_verdict

    emit_review_verdict(
        store,
        ReviewConfig(),
        pr_number=PR,
        item_id=ITEM,
        head_sha=HEAD,
        review_round=1,
        verdict=verdict,
        **extra,
    )


@pytest.mark.parametrize(
    "faults",
    [[], ["UNPARSEABLE", "DEGRADED"]],
    ids=["needs-changes", "needs-changes-then-faults"],
)
def test_a_standing_needs_changes_verdict_refuses_the_merge(
    store: Any, faults: list[str]
) -> None:
    """The drive's merge step refuses the same NEEDS_CHANGES fixture
    ``kew merge check`` refuses, even under a READY round for the head; a kew
    fault attested after it does not hide it."""
    _ready(store)
    _attest_verdict(store, "NEEDS_CHANGES")
    for fault in faults:
        _attest_verdict(store, fault, error_category="review_failed_no_verdict")
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == "merge-verdict-needs-changes"
    assert "NEEDS_CHANGES" in exc.value.detail
    assert gh.calls == []


def test_an_unknown_attested_verdict_refuses_the_merge(store: Any) -> None:
    _ready(store)
    _attest_verdict(store, "MAYBE")
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == "merge-verdict-unknown-verdict"
    assert gh.calls == []


def test_a_ready_verdict_after_needs_changes_merges(store: Any) -> None:
    _ready(store)
    _attest_verdict(store, "NEEDS_CHANGES")
    _attest_verdict(store, "READY")
    gh = _gh()

    outcome = _merge(store, run=gh)

    assert outcome.head == HEAD
    assert len(gh.calls) == 1


def test_an_accepted_needs_changes_verdict_still_merges(store: Any) -> None:
    """The stop rule's accepted residuals (#2112) read as READY under the
    verdict rule too: the attested NEEDS_CHANGES is the round it accepted. The
    acceptance is written as the stop rule writes it: the chain event, then the
    round's READY_WITH_RESIDUALS mark."""
    from kew.review_residuals import mark_round_accepted, record_acceptance

    round_num = store.record_review_round(
        EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", review_mode="full"
    )
    _attest_verdict(store, "NEEDS_CHANGES")
    record_acceptance(
        store,
        item_id=ITEM,
        repo=REPO,
        pr_number=PR,
        round_num=round_num,
        head_sha=HEAD,
        sidecar_digest=None,
        issue=99,
        after_rounds=round_num,
        findings=[],
    )
    latest = store.latest_review_round(ITEM)
    mark_round_accepted(store, ITEM, latest, issue=99)
    assert store.latest_review_round(ITEM).accepted_verdict == "READY_WITH_RESIDUALS"
    gh = _gh()

    outcome = _merge(store, run=gh)

    assert outcome.head == HEAD
    assert len(gh.calls) == 1


def test_a_standing_needs_full_verdict_refuses_the_merge(store: Any) -> None:
    """A delta READY that still owes a full round stops the drive's merge step
    with its own reason, not as an unknown verdict."""
    _ready(store)
    _attest_verdict(store, "NEEDS_FULL", review_mode="delta")
    gh = _gh()

    with pytest.raises(OperatorMergeRefused) as exc:
        _merge(store, run=gh)

    assert _refusal(exc) == "merge-verdict-needs-full"
    assert "still owes an independent full round at this head" in exc.value.detail
    assert gh.calls == []
