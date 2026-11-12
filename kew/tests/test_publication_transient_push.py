"""#2455: a push the REMOTE fails on its own side is retried, never redone.

The incident, on openshell-runtime #2322 (PR #2451): a review-remediation run
finished a 97-step fix, host publication pushed it, and GitHub answered

    remote: Internal Server Error ... [remote rejected] df93aeea...

The run was recorded ``publication_failed``, so the epic loop dispatched a
SECOND remediation to redo the whole fix from scratch — while the first one's
work sat preserved in a checkout on disk, one push away from landing.

Two defects, in the order the push hits them:

1. the review publication path sent the commit once and gave up, so a
   server-side answer ended the run (the dispatch push has retried since
   #1218; this path never did);
2. a server-side answer that survives every retry was reported as a broken
   publication, which the driver reads as "dispatch another remediation",
   instead of the ``publication_held`` every other preserved-work outcome earns.

A rejection the remote decided from the publication's own content or lineage —
a protected ref, a non-fast-forward, a push rule — is not retried: the wait
would buy the same answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import patch

import pytest
from support.driver_fakes import EPIC

from kew import publication_canonical, subprocess_utils, worktree
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import (
    PUBLICATION_HELD_HOLD_PREFIX,
    deferred_publication_key,
    holds_node_out_of_frontier,
    publication_held_hold_reason,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.publication import (
    PublicationError,
    TransientPushFailure,
    transient_push_rejection,
)
from kew.run_store import REVIEW_REMEDIATION_RUN_KIND, RunRecord, SqliteRunStore
from kew.worktree import PublicationHeadMovedError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ITEM = "2322"
PR = 2451
BRANCH = "agent/issue-2322"
NOW = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)
# The incident's own wording, as GitHub sent it.
SERVER_ERROR = "Internal Server Error"


# ---------------------------------------------------------------------------
# The classifier: which answers a retry can change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        f"canonical git push failed: remote: {SERVER_ERROR} "
        "! [remote rejected] df93aeea -> agent/issue-2322",
        "fatal: unable to access 'x': The requested URL returned error: 503",
        "error: RPC failed; HTTP 502 curl 22",
        "fatal: The remote end hung up unexpectedly",
        "! [remote rejected] abc -> main (unpacker error)",
        "ssh: connect to host port 22: Connection reset by peer",
    ],
    ids=(
        "internal-server-error",
        "http-5xx-transport",
        "rpc-failed",
        "hung-up",
        "remote-rejected-without-a-cause",
        "connection-reset",
    ),
)
def test_a_server_side_answer_is_transient(detail: str) -> None:
    assert transient_push_rejection(detail)


@pytest.mark.parametrize(
    "detail",
    [
        "! [rejected] abc -> main (non-fast-forward)",
        "! [rejected] abc -> main (stale info)",
        "! [rejected] abc -> main (fetch first)",
        "remote: error: GH006: Protected branch update failed",
        "remote: error: GH013: Repository rule violations found",
        "remote: error: push declined due to a detected secret",
        "remote: gitleaks found a credential in this push",
        "canonical git push failed: something nobody has classified",
    ],
    ids=(
        "non-fast-forward",
        "stale-lease",
        "fetch-first",
        "protected-branch",
        "push-rule",
        "secret-refusal",
        "gitleaks",
        "unknown-text",
    ),
)
def test_a_rejection_the_remote_decided_is_not_transient(detail: str) -> None:
    """Unknown text counts as settled too: a retry of an answer nobody has
    classified buys a wait and the same rejection, and the work is preserved
    either way."""
    assert not transient_push_rejection(detail)


def test_a_server_error_wins_over_the_generic_rejection_line_only_when_alone() -> None:
    """Every rejection carries ``[remote rejected]``, including the settled ones,
    so the settled markers are read first — otherwise a protected ref would be
    retried for a minute before failing exactly as it did on the first try."""
    assert not transient_push_rejection(
        "remote: error: GH006: Protected branch update failed\n"
        "! [remote rejected] abc -> main (protected branch hook declined)"
    )


# ---------------------------------------------------------------------------
# Defect 1: the review publication path retries a server-side answer
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production policy waits a minute; the proof is the retry, not the wait."""
    monkeypatch.setattr(worktree, "PUSH_DELAYS", (0.0, 0.0))


@pytest.fixture
def scene(tmp_path: Path) -> dict[str, Any]:
    """A PR branch on a real remote, plus a remediation checkout with a fix in it."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "fix.txt").write_text("before the review\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    head = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "remediation" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    (checkout / "fix.txt").write_text("the finished 97-step fix\n")
    return {"origin": origin, "seed": seed, "checkout": checkout, "head": head}


def _publish(scene: dict[str, Any]) -> Any:
    """The default path: exactly what a remediation's host publication calls."""
    return worktree.publish_existing_pr_branch(
        Path(scene["checkout"]),
        BRANCH,
        commit_message=f"fix: address review feedback on PR #{PR}",
        expected_remote_sha=str(scene["head"]),
        repo=str(scene["origin"]),
    )


@dataclass
class FakeRemote:
    """The remote's own answers to the host's pushes, scripted.

    A ``pre-receive`` hook would be the obvious fake, and is the one the
    publication suite already uses — but the sandbox image mounts the scratch
    filesystem ``noexec``, so a hook written there is silently skipped and every
    push succeeds. Answering the push itself works in both places and keeps
    production ``_push`` in the test: its error text, its credential redaction
    and the classification read off it are all the real ones.

    ``failures`` is how many of the next pushes the server fails before it
    starts accepting them; ``sent`` is every refspec the host actually sent, so
    a retry (or the absence of one) is counted rather than inferred.
    """

    failures: int = 0
    sent: list[str] = field(default_factory=list)

    @property
    def commits(self) -> list[str]:
        return [refspec.partition(":")[0] for refspec in self.sent]


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> FakeRemote:
    fake = FakeRemote()
    real_run = publication_canonical.run

    def answer(command: Any, **kwargs: Any) -> Any:
        if "push" not in command:
            return real_run(command, **kwargs)
        fake.sent.append(str(command[-1]))
        if fake.failures <= 0:
            return real_run(command, **kwargs)
        fake.failures -= 1
        return CompletedProcess(
            command,
            1,
            "",
            # The shape git prints for #2451's answer: the server's own line,
            # then the generic rejection every refused push carries.
            f"remote: {SERVER_ERROR}\n"
            f" ! [remote rejected] {command[-1]}\n"
            "error: failed to push some refs\n",
        )

    monkeypatch.setattr(publication_canonical, "run", answer)
    return fake


def test_one_server_error_is_retried_and_the_fix_lands(
    scene: dict[str, Any], remote: FakeRemote
) -> None:
    """AC1. The remote fails the first push and accepts the second. #2451 ended
    at the first answer, with the fix unpublished and a fresh remediation next."""
    remote.failures = 1

    published = _publish(scene)

    origin = Path(scene["origin"])
    assert published.commit_sha == _git(origin, "rev-parse", f"refs/heads/{BRANCH}")
    assert _git(origin, "show", f"{BRANCH}:fix.txt") == "the finished 97-step fix"
    assert remote.commits == [published.commit_sha, published.commit_sha], (
        "the retry re-sends the SAME commit — a second build would be a second "
        "publication intent for one round of work"
    )


def test_a_remote_that_keeps_failing_raises_a_transient_push_failure(
    scene: dict[str, Any], remote: FakeRemote
) -> None:
    """AC1, exhausted. Three tries, then an answer the caller can tell from a
    broken push — with the branch and the preserved checkout untouched."""
    remote.failures = publication_canonical.PUSH_ATTEMPTS + 1

    with pytest.raises(TransientPushFailure) as raised:
        _publish(scene)

    assert raised.value.attempts == publication_canonical.PUSH_ATTEMPTS
    assert SERVER_ERROR.lower() in raised.value.detail.lower()
    assert len(remote.sent) == publication_canonical.PUSH_ATTEMPTS
    assert len(set(remote.commits)) == 1, "one commit, sent three times"
    origin = Path(scene["origin"])
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}") == str(scene["head"])
    assert (Path(scene["checkout"]) / "fix.txt").read_text() == (
        "the finished 97-step fix\n"
    ), "the work the next push needs is still on disk"


def test_a_rejection_the_remote_decided_is_pushed_once(
    scene: dict[str, Any], remote: FakeRemote
) -> None:
    """AC3. The branch moves between the host's head query and its push, so the
    force-with-lease is stale — a non-fast-forward answer. Retrying it would
    re-send the same commit against the same lease, twice, for nothing."""
    seed = Path(scene["seed"])

    def move_the_branch() -> None:
        _git(seed, "checkout", "-q", "-B", BRANCH, str(scene["head"]))
        (seed / "other.txt").write_text("a concurrent push\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "concurrent")
        _git(seed, "push", "-q", "origin", BRANCH)

    with pytest.raises(PublicationHeadMovedError):
        worktree.publish_existing_pr_branch(
            Path(scene["checkout"]),
            BRANCH,
            commit_message=f"fix: address review feedback on PR #{PR}",
            expected_remote_sha=str(scene["head"]),
            repo=str(scene["origin"]),
            before_push=move_the_branch,
        )

    assert len(remote.sent) == 1, "a settled rejection is reported, not waited on"


# ---------------------------------------------------------------------------
# Defect 2: the exhausted push holds the node instead of redoing the work
# ---------------------------------------------------------------------------


def _remediation_row(run_id: str, *, status: str = "success") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=PR,
        item_id=ITEM,
        branch=BRANCH,
        status=status,
        exit_code=0,
        started_at=NOW,
        finished_at=NOW,
        run_kind=REVIEW_REMEDIATION_RUN_KIND,
        estimated_cost_usd=21.0,
    )


def _run_remediation_publication(
    store: Any, tmp_path: Path, *, head: str, failure: Exception
) -> tuple[Any, Path]:
    """One ``kew review`` remediation whose host publication raises *failure*."""
    from kew.review import dispatch_review
    from kew.review_checkout import ReviewCheckout
    from kew.review_contract import Finding
    from kew.tracking import DispatchResult

    checkout_dir = tmp_path / f"pr-{PR}"
    checkout_dir.mkdir()
    checkout = ReviewCheckout(
        path=checkout_dir,
        head_sha=head,
        branch=BRANCH,
        detached=False,
        impl_worktree=None,
        conflict=None,
    )
    store.save(_remediation_row("rem-1"))
    with (
        patch("kew.review._check_github_connectivity", return_value=True),
        patch(
            "kew.review._get_pr_metadata",
            return_value={
                "number": PR,
                "headRefName": BRANCH,
                "baseRefName": "main",
                "body": f"Closes #{ITEM}",
                "title": "Run an agent container on a gcp host",
            },
        ),
        patch("kew.review.materialize_review_checkout", return_value=checkout),
        patch("kew.review.ensure_publication_credentials"),
        patch(
            "kew.review.run_tracked_agent",
            return_value=DispatchResult(
                exit_code=0,
                issue_num=PR,
                log_file=tmp_path / "run.log",
                run_id="rem-1",
            ),
        ),
        patch("kew.review.publish_existing_pr_branch", side_effect=failure),
        patch("kew.review.publication_remote_sha", return_value=head),
        patch("kew.review.discard_checkout") as discarded,
    ):
        outcome = dispatch_review(
            pr_number=PR,
            config=DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "logs"))),
            store=store,
            item_id=ITEM,
            findings=[
                Finding(
                    severity="high",
                    location="src/kew/host.py:1",
                    summary="the dispatch is unproven",
                    evidence_command="uv run pytest -q",
                    evidence_output_digest="sha256:cafe",
                )
            ],
        )
    discarded.assert_not_called()
    return outcome, checkout_dir


def _exhausted() -> PublicationError:
    """The failure the canonical path hands its callers, wrapping as it does."""
    transient = TransientPushFailure(
        f"canonical git push failed: remote: {SERVER_ERROR} "
        f"! [remote rejected] df93aeea -> {BRANCH}",
        attempts=publication_canonical.PUSH_ATTEMPTS,
    )
    wrapped = PublicationError("push", transient)
    wrapped.__cause__ = transient
    return wrapped


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


def _drive(store: Any, tmp_path: Path, head: str) -> tuple[Any, list[Any]]:
    """The next review-stage pass over the node, remediator recording its calls."""
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
            round_count=2,
            latest_verdict="NEEDS_CHANGES",
            latest_head_sha=head,
            current_head_sha=head,
        ),
    )
    outcome = drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=DispatchConfig(
            review=ReviewConfig(adversarial=True, blocking=True, max_rounds=8),
            logging=LoggingConfig(dir=str(tmp_path / "logs")),
        ),
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        remediator=calls.append,
        now=NOW,
    )
    return outcome, calls


def test_an_exhausted_push_holds_the_node_and_dispatches_nothing(
    tmp_path: Path,
) -> None:
    """AC2. The whole #2455 loop, end to end: the run is held with its preserved
    checkout named, and the tick that follows dispatches no second remediation."""
    head = "df93aeea" * 5
    with SqliteRunStore(str(tmp_path / "t.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        outcome, checkout_dir = _run_remediation_publication(
            store, tmp_path, head=head, failure=_exhausted()
        )

        assert outcome.exit_code == 1
        row = store.get("rem-1")
        assert row is not None
        assert row.status == "awaiting_publication", (
            "a push the remote failed published nothing and lost nothing: the "
            "work is intact, so the row the driver reads must not say success"
        )
        assert row.error_category == PUBLICATION_HELD_HOLD_PREFIX
        evidence = json.loads(store.get_meta(deferred_publication_key("rem-1")) or "{}")
        assert evidence == {"head": head, "checkout": str(checkout_dir), "pr": PR}
        assert checkout_dir.is_dir(), "the preserved fix is what the hold points at"

        held, calls = _drive(store, tmp_path, head)

        assert calls == [], (
            "#2451 dispatched a second remediation here, to redo a 97-step fix "
            "that was already sitting in the preserved checkout"
        )
        assert held.held == [ITEM]
        assert held.remediating == []
        reasons = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reasons == [publication_held_hold_reason(str(checkout_dir))]
        assert holds_node_out_of_frontier(reasons[0])


def test_a_broken_publication_is_still_a_failure(tmp_path: Path) -> None:
    """The control. Only a server-side answer earns the hold: a push that broke
    for any other reason still fails the run, as it did before #2455."""
    head = "deadbeef" * 5
    with SqliteRunStore(str(tmp_path / "t.db")) as store:
        outcome, _ = _run_remediation_publication(
            store,
            tmp_path,
            head=head,
            failure=PublicationError("push", "canonical git push failed: no such ref"),
        )

        assert outcome.exit_code == 1
        row = store.get("rem-1")
        assert row is not None
        assert row.status == "failure"
        assert row.error_category == "publication_failed"
        assert store.get_meta(deferred_publication_key("rem-1")) is None
