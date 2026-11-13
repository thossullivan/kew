"""A zero-diff exit records a candidate and closes nothing (I10B / Task 45).

``_complete_no_changes`` used to *complete* things: it labelled and closed the
GitHub issue, auto-unblocked the dependents, and — for a Linear item riding the
``issue_num=0`` sentinel — called a tracker hook that commented and closed the
item outright. All of that was decided by an absence. "The agent looked and
there was genuinely nothing to do" and "the agent never got as far as writing a
file" produce the same empty diff, so the legacy path routinely read the second
as the first and moved an epic forward on the strength of it.

Now both zero-change paths — host broker and contained run — record ONE durable
no-change candidate (:mod:`kew.no_change`) and decide nothing: no close, no
comment, no unblock. What the absence meant is a decision I11 asks a person to
make. The one write left is the dispatch claim the finished run no longer holds
(#1611) — see :class:`TestTheDispatchClaimIsReleased`.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew.artifact_receipt import ArtifactLineage, ArtifactReceipt
from kew.audit import Actor
from kew.config import DispatchConfig
from kew.dispatch_core import _complete_no_changes
from kew.evidence import EvidenceKind, Producer, ProducerKind, record_evidence
from kew.no_change import (
    AUTHORIZED,
    CANDIDATE,
    PENDING_CONFIRMATION,
    TERMINAL_STATES,
)
from kew.publication_artifact import WORKSPACE_SCOPE
from kew.run_store import RunRecord, SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS
from kew.tracking import DispatchResult

_P = "kew.dispatch_core"


@pytest.fixture(autouse=True)
def _git_allowlisted(monkeypatch: Any) -> None:
    """The dispatch allowlist as a real run has it (``preflight_tools``)."""
    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A dispatch workspace whose ``origin/<base>`` resolves, like a worktree."""
    repo = tmp_path / "wt"
    repo.mkdir()
    git = TOOL_PATHS["git"]

    def g(*args: str) -> None:
        subprocess.run([git, *args], cwd=repo, capture_output=True, check=True)

    g("init", "-b", "main")
    g("config", "user.name", "t")
    g("config", "user.email", "t@t")
    g("commit", "--allow-empty", "-m", "base")
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


@pytest.fixture(autouse=True)
def _private_project(tmp_path: Path, monkeypatch: Any) -> None:
    """A project root of this test's own.

    The seal (#1610) preserves its empty work package under
    ``<project>/.kew/private/packages``, so a test that left this unset would
    write real bytes into the checkout kew is running from.
    """
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KEW_PROJECT", str(project))


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _record_run(store: Any, *, run_id: str, item_id: str = "42") -> None:
    """A finished run row, so the judgment binds to a real attempt."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id) if item_id.isdigit() else 0,
            item_id=item_id,
            branch="agent/issue-42",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
        )
    )


def _replayable_ledger(store: Any, *, run_id: str) -> None:
    """One evidence row for this run, so its ledger replays with something to
    say. Without it, attempted-change evidence reads as UNAVAILABLE and the
    candidate is unattestable for a reason that has nothing to do with the
    package under test."""
    record_evidence(
        store,
        EvidenceKind.RECEIPT_FACT,
        actor=Actor("policy", "host-dispatch"),
        producer=Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="t"),
        run_id=run_id,
        facts={"fact": "outcome", "value": "no_changes"},
    )


def _seal_real_work(store: Any, checkout: Path) -> ArtifactReceipt:
    """A receipt naming bytes some other path already sealed for this attempt."""
    lineage = ArtifactLineage(
        source_kind="github",
        source_id="42",
        run_id="run-1",
        attempt_number=1,
        base_sha="c" * 40,
        package_digest="sha256:" + "a" * 64,
        tree_sha="b" * 40,
        verification="exact_tree",
    )
    sealed: ArtifactReceipt = store.seal_artifact_receipt(
        lineage=lineage, sealed_at=datetime(2026, 8, 24, 11, tzinfo=UTC)
    )
    return sealed


def _complete(
    store: Any,
    checkout: Path,
    *,
    item_id: str = "42",
    provider: str = "github",
    run_id: str = "run-1",
) -> Any:
    # Canonical publication binds the judgment to the base recorded before
    # the agent ran; the test reads it with git, which the host does not.
    try:
        base = subprocess.run(
            [TOOL_PATHS["git"], "rev-parse", "HEAD"],
            cwd=checkout,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        base = ""  # an unreadable workspace records no base, and no judgment
    # This helper models the checkout-mode host path (origin/<base> read in
    # the workspace); canonical mode records a judgment only from walker
    # evidence, which the contained-path tests below supply.
    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    return _complete_no_changes(
        config,
        item_id=item_id,
        provider=provider,
        store=store,
        run_id=run_id,
        worktree_dir=checkout,
        trusted_base_sha=base,
    )


class TestCandidateCreation:
    def test_the_host_path_records_a_candidate_and_writes_nothing(
        self, store: Any, checkout: Path, monkeypatch: Any
    ) -> None:
        """The whole point: a durable judgment, and not one remote call."""
        commands: list[list[str]] = []
        real = __import__("kew.dispatch_core", fromlist=["run"]).run

        def _spy(cmd: list[str], **kwargs: Any) -> Any:
            commands.append(list(cmd))
            return real(cmd, **kwargs)

        monkeypatch.setattr(f"{_P}.run", _spy)
        _record_run(store, run_id="run-1")

        judgment = _complete(store, checkout)

        assert judgment is not None
        assert judgment.state == CANDIDATE
        assert judgment.lineage_key == "github:42"
        assert [c for c in commands if c[0] != "git"] == []
        # Nothing was published, so nothing is queued for a human reviewer:
        # a candidate earns no PR, no merge and no review-WIP credit.
        assert store.list_publication_jobs() == []
        assert store.get("run-1").pr_url is None

    def test_a_linear_item_takes_the_same_path_as_a_github_one(
        self, store: Any, checkout: Path
    ) -> None:
        """No tracker hook, no ``issue_num=0`` sentinel — one shared seam."""
        _record_run(store, run_id="run-1", item_id="SFD-42")

        judgment = _complete(
            store, checkout, item_id="SFD-42", provider="linear", run_id="run-1"
        )

        assert judgment is not None
        assert judgment.state == CANDIDATE
        assert judgment.lineage_key == "linear:SFD-42"

    def test_the_contained_path_converges_on_the_same_candidate(
        self, store: Any, checkout: Path
    ) -> None:
        """Host and container record ONE judgment for one run attempt, so a
        retry after a crash resumes the outcome instead of forking it."""
        _record_run(store, run_id="run-1")

        first = _complete(store, checkout)
        second = _complete(store, checkout)

        assert first is not None and second is not None
        assert first.judgment_id == second.judgment_id
        assert len(store.list_no_change_judgments()) == 1


class TestNoAutomaticClosure:
    def test_missing_attempted_change_evidence_cannot_auto_close(
        self, store: Any, checkout: Path, capsys: Any
    ) -> None:
        """A run with no replayable attempted-change ledger stays a candidate.

        Suppressed work is the sharpest case an empty diff hides, so evidence
        that cannot be read is a gap — never a silent "nothing was attempted".
        """
        _record_run(store, run_id="run-1")

        judgment = _complete(store, checkout)

        assert judgment is not None
        assert judgment.state == CANDIDATE
        assert judgment.state not in TERMINAL_STATES
        assert judgment.decided_by is None
        captured = capsys.readouterr()
        assert "is not attested" in captured.out + captured.err
        assert "attempted" in captured.out + captured.err

    def test_a_restart_neither_forks_the_outcome_nor_frees_a_dependent(
        self, store: Any, checkout: Path
    ) -> None:
        """A second zero-change run on the same item finds the live judgment
        and leaves it live — it never opens a rival lineage a driver could
        read as settled."""
        _record_run(store, run_id="run-1")
        _record_run(store, run_id="run-2")

        first = _complete(store, checkout, run_id="run-1")
        second = _complete(store, checkout, run_id="run-2")

        assert first is not None
        assert second is None
        live = store.live_no_change_judgment("github:42")
        assert live is not None
        assert live.judgment_id == first.judgment_id
        assert live.state == CANDIDATE


class TestRefusalsAreLoudNotLossy:
    def test_no_store_records_nothing_and_still_writes_nothing(
        self, checkout: Path, capsys: Any
    ) -> None:
        judgment = _complete_no_changes(
            DispatchConfig(),
            item_id="42",
            provider="github",
            store=None,
            run_id="run-1",
            worktree_dir=checkout,
        )
        assert judgment is None
        assert "no no-change outcome was recorded" in capsys.readouterr().err

    def test_an_unreadable_base_refuses_to_invent_a_judgment(
        self, store: Any, tmp_path: Path
    ) -> None:
        """The judgment is bound to the base the run worked from. Without one
        there is nothing to bind, and a fabricated base would be worse than no
        record at all."""
        _record_run(store, run_id="run-1")

        judgment = _complete(store, tmp_path / "not-a-repo")

        assert judgment is None
        assert store.list_no_change_judgments() == ()


class TestContainedDispatchLandsTheCandidateHostSide:
    """A real contained zero-change dispatch persists the candidate (#1606).

    The container classifies the empty diff host-side and hands the
    classification back as ``sandbox_no_change_needed`` on an exit-0
    ``DispatchResult``. Collection used to read *any* error category as a
    failure, so the contained path never reached the no-change seam at all:
    no candidate row, the item labelled failed, and an operator told to retry
    a run that was correct. This is the end-to-end wire #1586 (item 2) flagged
    as untested.
    """

    @staticmethod
    def _ctx(dispatch_core: Any, store: Any, tmp_path: Path) -> Any:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        return dispatch_core._DispatchContext(
            config=config,
            store=store,
            notifier=None,
            force_budget=False,
            approved=True,
            interactive=False,
            model_override=None,
            merge_context=None,
            epic_ref=None,
            provider="github",
            issue_num=42,
            issue_title="Nothing to do",
            issue_body="",
            complexity="simple",
            item_id="42",
            branch="agent/issue-42",
            publication_base_sha="c" * 40,
            start_monotonic=0.0,
            refuse_rollback=lambda: None,
            terminal_failure=MagicMock(),
            run_kind="dispatch",
        )

    @staticmethod
    def _result(checkout: Path) -> Any:
        from types import SimpleNamespace

        from kew.publication_canonical import CanonicalNoChange

        # The contained path carries the walker's verified empty package.
        evidence = CanonicalNoChange(
            artifact=SimpleNamespace(
                entries=(), deletions=(), base_sha="c" * 40, content=lambda _e: b""
            ),
            base_sha="c" * 40,
            tree_sha="b" * 40,
        )
        return DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=checkout / "run.log",
            run_id="run-1",
            error_category="sandbox_no_change_needed",
            canonical_no_change=evidence,
        )

    def _collect(self, store: Any, checkout: Path, tmp_path: Path) -> Any:
        import kew.dispatch_core as dispatch_core

        ctx = self._ctx(dispatch_core, store, tmp_path)
        with patch(f"{_P}._cleanup_dispatch_dir") as cleanup:
            outcome = dispatch_core._collect_result(
                ctx,
                result=self._result(checkout),
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )
        return ctx, outcome, cleanup

    def test_a_contained_no_change_run_persists_a_candidate(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        _record_run(store, run_id="run-1")

        _, outcome, _ = self._collect(store, checkout, tmp_path)

        judgments = store.list_no_change_judgments()
        assert len(judgments) == 1
        assert judgments[0].state == CANDIDATE
        assert judgments[0].run_id == "run-1"
        assert outcome.exit_code == 0

    def test_a_contained_no_change_run_is_never_reported_as_a_failure(
        self, store: Any, checkout: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """Exit 0 with nothing to publish is an outcome, not a failed dispatch:
        no failure label, and no "retry fresh" steer off a correct run."""
        _record_run(store, run_id="run-1")

        ctx, _, _ = self._collect(store, checkout, tmp_path)

        ctx.terminal_failure.assert_not_called()
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "Dispatch failed" not in combined
        assert "retry fresh" not in combined
        assert "Recorded no-change candidate" in combined
        assert "the item stays open until the outcome is confirmed" in combined

    def test_the_no_change_category_on_a_nonzero_exit_never_records_a_candidate(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """The no-change collection is a *success* outcome. The category is only
        emitted with exit 0 today, but should it ever arrive on a failed run the
        guard must fall through to the hard-fail ladder, not persist a candidate
        for a run that did not actually succeed."""
        import kew.dispatch_core as dispatch_core

        _record_run(store, run_id="run-1")
        ctx = self._ctx(dispatch_core, store, tmp_path)
        failed = DispatchResult(
            exit_code=1,
            issue_num=42,
            log_file=checkout / "run.log",
            run_id="run-1",
            error_category="sandbox_no_change_needed",
        )
        with (
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch(f"{_P}._collect_hard_fail_or_rescue") as hard_fail,
            patch(f"{_P}._record_no_change") as record,
        ):
            dispatch_core._collect_result(
                ctx,
                result=failed,
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )

        hard_fail.assert_called_once()
        record.assert_not_called()
        assert store.list_no_change_judgments() == ()

    def test_the_contained_checkout_is_cleaned_up_as_a_sandbox_clone(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """A correct run leaves no workspace behind for "incident review", and
        a sandbox clone must be removed as a clone, never as a worktree."""
        _record_run(store, run_id="run-1")

        _, _, cleanup = self._collect(store, checkout, tmp_path)

        cleanup.assert_called_once_with(
            checkout, sandboxed=True, branch="agent/issue-42"
        )

    def test_the_candidate_is_recorded_before_the_checkout_is_removed(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """The judgment is bound to the base sha read out of that checkout, so
        cleaning up first would silently refuse to record anything."""
        _record_run(store, run_id="run-1")
        import kew.dispatch_core as dispatch_core

        ctx = self._ctx(dispatch_core, store, tmp_path)
        with patch(
            f"{_P}._cleanup_dispatch_dir",
            side_effect=lambda path, **kw: shutil.rmtree(path, ignore_errors=True),
        ):
            dispatch_core._collect_result(
                ctx,
                result=self._result(checkout),
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )

        assert len(store.list_no_change_judgments()) == 1


class TestTheCandidateLandsAttested:
    """The seam seals the evidence attestation reads (#1610).

    #1608 landed the candidate; it landed with ``evidence_json`` NULL, because
    neither zero-change path ever sealed the work package
    ``_attest_no_change`` reads a trusted base and a ``workspace`` package off.
    An un-attested candidate is not merely evidence-poor: ``confirm_no_change``
    refuses anything that is not ``pending_confirmation``, so the whole
    confirm -> tracker-sync leg was unreachable.
    """

    def test_a_zero_change_run_lands_attested_and_awaiting_a_person(
        self, store: Any, checkout: Path
    ) -> None:
        _record_run(store, run_id="run-1")
        _replayable_ledger(store, run_id="run-1")

        judgment = _complete(store, checkout)

        assert judgment is not None
        assert judgment.state == PENDING_CONFIRMATION
        assert judgment.evidence_digest
        assert judgment.evidence is not None
        assert judgment.evidence.gaps() == ()

    def test_the_evidence_names_the_empty_workspace_package_that_was_sealed(
        self, store: Any, checkout: Path
    ) -> None:
        """Not a bare "attested" flag: the evidence points at a real package
        whose bytes a reviewer can go and re-read."""
        _record_run(store, run_id="run-1")
        _replayable_ledger(store, run_id="run-1")

        judgment = _complete(store, checkout)

        assert judgment is not None
        evidence = judgment.evidence
        assert evidence is not None
        assert evidence.package_scope == WORKSPACE_SCOPE
        assert evidence.package_entry_count == 0
        assert evidence.package_deletion_count == 0
        assert evidence.trusted_base_sha == judgment.base_sha
        assert evidence.observed_base_sha == judgment.base_sha
        receipt = store.get_artifact_receipt(run_id="run-1", attempt_number=1)
        assert receipt is not None
        assert receipt.package_digest == evidence.package_digest

    def test_a_person_can_confirm_the_attested_outcome(
        self, store: Any, checkout: Path
    ) -> None:
        """The gate the whole fix is for: confirmation is now admissible."""
        _record_run(store, run_id="run-1")
        _replayable_ledger(store, run_id="run-1")
        judgment = _complete(store, checkout)
        assert judgment is not None

        confirmed = store.confirm_no_change(
            judgment_id=judgment.judgment_id,
            actor_id="operator",
            now=datetime(2026, 8, 24, 13, tzinfo=UTC),
        )

        assert confirmed.state == AUTHORIZED
        assert confirmed.decided_by == "operator"

    def test_a_workspace_that_still_carries_work_stays_an_unattested_candidate(
        self, store: Any, checkout: Path, capsys: Any
    ) -> None:
        """The refusal that keeps the seal honest: an agent that wrote files
        without committing them must not be sealed as an empty package."""
        _record_run(store, run_id="run-1")
        _replayable_ledger(store, run_id="run-1")
        (checkout / "left-behind.py").write_text("print('work')\n")

        judgment = _complete(store, checkout)

        assert judgment is not None
        assert judgment.state == CANDIDATE
        assert judgment.evidence is None
        assert store.get_artifact_receipt(run_id="run-1", attempt_number=1) is None
        captured = capsys.readouterr()
        assert "uncommitted path(s)" in captured.out + captured.err

    def test_a_receipt_that_already_names_real_work_is_never_overwritten(
        self, store: Any, checkout: Path
    ) -> None:
        """A sealed publication receipt wins: the empty package must never
        replace one that names bytes this attempt actually produced."""
        _record_run(store, run_id="run-1")
        _replayable_ledger(store, run_id="run-1")
        sealed = _seal_real_work(store, checkout)

        judgment = _complete(store, checkout)

        assert judgment is not None
        assert judgment.state == CANDIDATE
        receipt = store.get_artifact_receipt(run_id="run-1", attempt_number=1)
        assert receipt is not None
        assert receipt.package_digest == sealed.package_digest


class TestTheDispatchClaimIsReleased:
    """A finished no-change run hands the item back (#1611).

    Recording the candidate settled the *outcome*; the dispatch *claim* was
    left standing. The item kept ``in-progress`` + ``agent-working`` forever
    after a correct exit-0 run, so ``kew dispatch`` refused a redispatch as
    "already in progress" and ``kew cleanup`` read a finished run as an orphan.
    The release decides nothing on its own: no close, no comment, no unblock —
    confirming or rejecting the absence stays the human decision (I11).
    """

    @staticmethod
    def _gh_calls(mock_run: MagicMock) -> list[list[str]]:
        return [call.args[0] for call in mock_run.call_args_list]

    @classmethod
    def _label_edit(cls, mock_run: MagicMock) -> list[str]:
        edits = [c for c in cls._gh_calls(mock_run) if c[:3] == ["gh", "issue", "edit"]]
        assert len(edits) == 1, f"expected exactly one relabel, got {edits}"
        return edits[0]

    @staticmethod
    def _flags(cmd: list[str], flag: str) -> set[str]:
        return {cmd[i + 1] for i, tok in enumerate(cmd) if tok == flag}

    def _collect(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> tuple[Any, MagicMock]:
        import kew.dispatch_core as dispatch_core

        contained = TestContainedDispatchLandsTheCandidateHostSide()
        ctx = contained._ctx(dispatch_core, store, tmp_path)
        with (
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch("kew.labels.run") as gh,
        ):
            outcome = dispatch_core._collect_result(
                ctx,
                result=contained._result(checkout),
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )
        return outcome, gh

    def test_the_working_claim_is_cleared_and_the_no_change_label_applied(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        _record_run(store, run_id="run-1")

        _, gh = self._collect(store, checkout, tmp_path)

        cmd = self._label_edit(gh)
        assert self._flags(cmd, "--remove-label") == {
            "in-progress",
            "agent-working",
            "ready",
        }
        assert self._flags(cmd, "--add-label") == {"agent-completed-no-changes"}

    def test_a_failure_outcome_never_acquires_the_no_change_label(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """Only an exit-0 no-change hands back as a candidate. A real failure
        (here: killed) must never acquire agent-completed-no-changes. The
        isolation is structural today (failure collectors do not call
        _record_no_change), so lock it against a future refactor."""
        import kew.dispatch_core as dispatch_core

        _record_run(store, run_id="run-1")
        contained = TestContainedDispatchLandsTheCandidateHostSide()
        ctx = contained._ctx(dispatch_core, store, tmp_path)
        killed = DispatchResult(
            exit_code=137,
            issue_num=42,
            log_file=checkout / "run.log",
            run_id="run-1",
            error_category="killed",
        )
        with (
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch("kew.labels.run"),
            patch(f"{_P}.mark_no_change_candidate") as relabel,
        ):
            dispatch_core._collect_result(
                ctx,
                result=killed,
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )

        relabel.assert_not_called()
        assert store.list_no_change_judgments() == ()

    def test_releasing_the_claim_still_closes_and_comments_on_nothing(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """The label is a hand-back, not a verdict: the item stays open and
        unannotated until a person confirms or rejects the candidate."""
        _record_run(store, run_id="run-1")

        _, gh = self._collect(store, checkout, tmp_path)

        verbs = [c[2] for c in self._gh_calls(gh) if c[:2] == ["gh", "issue"]]
        assert "close" not in verbs
        assert "comment" not in verbs

    def test_the_candidate_is_durable_before_the_claim_is_released(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """Relabelling first would advertise a settled outcome the ledger does
        not yet carry — a crash in between would strand the item unclaimed with
        no candidate to confirm."""
        import kew.dispatch_core as dispatch_core

        _record_run(store, run_id="run-1")
        seen: list[int] = []
        contained = TestContainedDispatchLandsTheCandidateHostSide()
        ctx = contained._ctx(dispatch_core, store, tmp_path)
        with (
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch(
                f"{_P}.mark_no_change_candidate",
                side_effect=lambda *a, **kw: seen.append(
                    len(store.list_no_change_judgments())
                ),
            ),
        ):
            dispatch_core._collect_result(
                ctx,
                result=contained._result(checkout),
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )

        assert seen == [1]

    def test_a_tracker_that_refuses_the_relabel_never_fails_a_correct_run(
        self, store: Any, checkout: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """Exit 0 with nothing to publish stays exit 0, and the unreleased
        claim is said out loud rather than defaulted to clean."""
        import kew.dispatch_core as dispatch_core

        _record_run(store, run_id="run-1")
        contained = TestContainedDispatchLandsTheCandidateHostSide()
        ctx = contained._ctx(dispatch_core, store, tmp_path)
        with (
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch(
                f"{_P}.mark_no_change_candidate",
                side_effect=RuntimeError("gh issue edit failed (exit 1): nope"),
            ),
        ):
            outcome = dispatch_core._collect_result(
                ctx,
                result=contained._result(checkout),
                worktree_dir=checkout,
                run_sandboxed_path=True,
            )

        assert outcome.exit_code == 0
        assert len(store.list_no_change_judgments()) == 1
        combined = capsys.readouterr()
        assert "claim could not be released" in combined.out + combined.err

    def test_the_host_broker_zero_change_path_releases_the_same_claim(
        self, store: Any, checkout: Path, tmp_path: Path
    ) -> None:
        """Both zero-change paths go through one seam: a host run whose broker
        found nothing to publish hands the item back exactly as a contained one
        does, instead of two dialects of "done"."""
        import kew.dispatch_core as dispatch_core

        _record_run(store, run_id="run-1")
        contained = TestContainedDispatchLandsTheCandidateHostSide()
        ctx = contained._ctx(dispatch_core, store, tmp_path)
        with (
            patch(f"{_P}._broker_agent_work", return_value=False),
            patch("kew.labels.run") as gh,
        ):
            early = dispatch_core._broker_or_complete(
                ctx,
                contained._result(checkout),
                checkout,
                False,
            )

        assert early is None
        cmd = self._label_edit(gh)
        assert "agent-working" in self._flags(cmd, "--remove-label")
        assert self._flags(cmd, "--add-label") == {"agent-completed-no-changes"}
