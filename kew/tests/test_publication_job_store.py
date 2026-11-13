"""I08: one publication job owns commit, push, open and reconcile intent.

Publication used to be a thing that either happened during a dispatch or did
not, leaving `awaiting_publication` behind as the only trace. That encoding
cannot answer the question a host actually has after a crash — *did the push
land?* — so this table records the intent BEFORE the effect and the observation
AFTER it, as one normalized lifecycle per lineage.

Three properties are asserted here rather than assumed.

* **A job never outruns its sealed receipt (I07).** The intent must name a
  receipt that exists and agree with the base and tree that receipt sealed;
  otherwise the job would be a plan to publish something nobody attested to.
* **One lineage has at most one active job.** A successor SHA supersedes its
  predecessor in the same transaction that creates it, so two workers can never
  hold two live intents for one branch.
* **Nothing here can publish yet.** The selector that would hand a job to a
  worker is closed until E7 installs publication capacity, and no dispatch or
  product verb names this module at all.

Legacy rows are decoded, never rewritten: an `awaiting_publication` run reads
as ambiguous (or as an open pull request when it named one) without a row ever
being written on its behalf.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.publication_job import (
    ABANDONED,
    AMBIGUOUS,
    COMMITTED,
    OPENED,
    PENDING,
    PUSHED,
    RECONCILED,
    REMOTE_ABSENT,
    REMOTE_BRANCH,
    REMOTE_PULL_REQUEST,
    REMOTE_SETTLED,
    REMOTE_UNKNOWN,
    SUPERSEDED,
    PublicationIntent,
    PublicationJobConflictError,
    PublicationJobsInactiveError,
    PublicationJobStateError,
    PublicationJobValueError,
    RemoteObservation,
    decode_legacy_publication,
    publication_jobs_active,
    resolve_observation,
    validate_transition,
)
from kew.publication_repository import VERIFICATION_EXACT_TREE
from kew.run_store import ArtifactLineage, SqliteRunStore

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
_PACKAGE = "sha256:" + "a" * 64
_BASE = "b" * 40
_TREE = "c" * 40
_HEAD = "d" * 40


def _store(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(str(tmp_path / "dispatch.db"))


def _seal(
    store: SqliteRunStore,
    *,
    run_id: str = "run-1",
    attempt_number: int = 1,
    base_sha: str = _BASE,
    tree_sha: str = _TREE,
) -> str:
    receipt = store.seal_artifact_receipt(
        lineage=ArtifactLineage(
            source_kind="github",
            source_id="acme/widgets",
            run_id=run_id,
            attempt_number=attempt_number,
            base_sha=base_sha,
            package_digest=_PACKAGE,
            tree_sha=tree_sha,
            verification=VERIFICATION_EXACT_TREE,
            locator="packages/" + "a" * 64,
        ),
        sealed_at=_NOW,
    )
    return receipt.receipt_digest


def _intent(receipt_digest: str, **overrides: object) -> PublicationIntent:
    fields: dict[str, object] = {
        "source_kind": "github",
        "source_id": "acme/widgets",
        "run_id": "run-1",
        "attempt_number": 1,
        "branch": "agent/issue-7",
        "base_sha": _BASE,
        "tree_sha": _TREE,
        "receipt_digest": receipt_digest,
        "message": "feat(#7): the change the agent made",
    }
    fields.update(overrides)
    return PublicationIntent(**fields)  # type: ignore[arg-type]


def _opened(store: SqliteRunStore, **overrides: object) -> object:
    digest = _seal(
        store,
        run_id=str(overrides.get("run_id", "run-1")),
        attempt_number=int(overrides.get("attempt_number", 1)),  # type: ignore[call-overload]
    )
    return store.open_publication_job(intent=_intent(digest, **overrides), now=_NOW)


class TestTheIntentIsDurableBeforeAnythingHappens:
    """The row exists before the commit, the push, or the pull request does."""

    def test_a_new_job_records_every_field_the_publication_will_need(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)

        assert job.state == PENDING
        assert job.branch == "agent/issue-7"
        assert job.base_sha == _BASE
        assert job.tree_sha == _TREE
        assert job.message == "feat(#7): the change the agent made"
        assert job.attempt_count == 0
        assert job.head_sha is None
        assert job.active

    def test_the_lifecycle_survives_reopening_the_database(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            store.record_publication_attempt(job_id=job.job_id, step="commit", now=_NOW)
            store.mark_publication_state(
                job_id=job.job_id,
                state=COMMITTED,
                expected_state=PENDING,
                head_sha=_HEAD,
                now=_NOW,
            )

        with _store(tmp_path) as reopened:
            reread = reopened.get_publication_job(job.job_id)

        assert reread is not None
        assert reread.state == COMMITTED
        assert reread.head_sha == _HEAD
        assert reread.attempt_count == 1
        assert reread.last_step == "commit"

    def test_an_attempt_is_durable_before_the_effect_it_describes(
        self, tmp_path: Path
    ) -> None:
        """Crash between "about to push" and the push: the row remembers."""
        with _store(tmp_path) as store:
            job = _opened(store)
            store.mark_publication_state(
                job_id=job.job_id,
                state=COMMITTED,
                expected_state=PENDING,
                head_sha=_HEAD,
                now=_NOW,
            )
            store.record_publication_attempt(job_id=job.job_id, step="push", now=_LATER)

        with _store(tmp_path) as reopened:
            reread = reopened.get_publication_job(job.job_id)

        assert reread is not None
        assert reread.state == COMMITTED
        assert reread.last_step == "push"
        assert reread.last_attempt_at == _LATER.isoformat()

    def test_a_job_is_readable_by_the_attempt_that_produced_it(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            found = store.publication_job_for_attempt(run_id="run-1", attempt_number=1)
            missing = store.publication_job_for_attempt(
                run_id="run-1", attempt_number=2
            )

        assert found is not None and found.job_id == job.job_id
        assert missing is None

    def test_reopening_the_same_intent_returns_the_same_job(
        self, tmp_path: Path
    ) -> None:
        """A retry after a crash converges instead of forking the lineage."""
        with _store(tmp_path) as store:
            first = _opened(store)
            digest = store.get_artifact_receipt(
                run_id="run-1", attempt_number=1
            ).receipt_digest
            second = store.open_publication_job(intent=_intent(digest), now=_LATER)

            assert second.job_id == first.job_id
            assert second.created_at == first.created_at
            assert len(store.list_publication_jobs()) == 1


class TestAJobCannotOutrunItsSealedReceipt:
    """I07 sealed what the host would publish; the job may not contradict it."""

    def test_an_intent_without_a_sealed_receipt_is_refused(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store, pytest.raises(PublicationJobValueError) as err:
            store.open_publication_job(intent=_intent("f" * 64), now=_NOW)

        assert "receipt" in str(err.value)
        with _store(tmp_path) as store:
            assert store.list_publication_jobs() == []

    def test_an_intent_naming_a_different_receipt_digest_is_refused(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            _seal(store)
            with pytest.raises(PublicationJobValueError):
                store.open_publication_job(intent=_intent("e" * 64), now=_NOW)

    def test_an_intent_that_disagrees_with_the_sealed_tree_is_refused(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            digest = _seal(store)
            with pytest.raises(PublicationJobValueError) as err:
                store.open_publication_job(
                    intent=_intent(digest, tree_sha="9" * 40), now=_NOW
                )

        assert "tree" in str(err.value)

    def test_an_intent_claiming_a_foreign_source_kind_is_refused(
        self, tmp_path: Path
    ) -> None:
        # The receipt is sealed for github:acme/widgets; an intent may not copy
        # its digest/tree/base while binding a foreign source — the lineage_key
        # is built from the intent's own source, so this would let a job claim a
        # lineage the sealed evidence never attested.
        with _store(tmp_path) as store:
            digest = _seal(store)
            with pytest.raises(PublicationJobValueError) as err:
                store.open_publication_job(
                    intent=_intent(digest, source_kind="attacker"), now=_NOW
                )
            assert "source" in str(err.value)
            assert store.list_publication_jobs() == []

    def test_an_intent_claiming_a_foreign_source_id_is_refused(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            digest = _seal(store)
            with pytest.raises(PublicationJobValueError) as err:
                store.open_publication_job(
                    intent=_intent(digest, source_id="evil/fork"), now=_NOW
                )
            assert "source" in str(err.value)
            assert store.list_publication_jobs() == []

    def test_an_intent_missing_a_branch_is_refused_before_any_write(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            digest = _seal(store)
            with pytest.raises(PublicationJobValueError):
                store.open_publication_job(intent=_intent(digest, branch=""), now=_NOW)
            assert store.list_publication_jobs() == []

    def test_a_different_intent_for_the_same_attempt_is_a_conflict(
        self, tmp_path: Path
    ) -> None:
        """The id is derived from the lineage, so it can be reached twice."""
        with _store(tmp_path) as store:
            job = _opened(store)
            digest = store.get_artifact_receipt(
                run_id="run-1", attempt_number=1
            ).receipt_digest
            with pytest.raises(PublicationJobConflictError) as err:
                store.open_publication_job(
                    intent=_intent(digest, message="a different commit"), now=_LATER
                )
            assert store.get_publication_job(job.job_id).message.startswith("feat")

        assert "different message" in str(err.value)


class TestOneLineageHasOneActiveJob:
    """Two live intents for one branch is the race publication cannot survive."""

    def test_a_second_active_job_for_one_lineage_is_refused(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            _opened(store)
            digest = _seal(store, run_id="run-2", attempt_number=1)
            with pytest.raises(PublicationJobConflictError) as err:
                store.open_publication_job(
                    intent=_intent(digest, run_id="run-2"), now=_LATER
                )

        assert "active" in str(err.value)

    def test_a_successor_sha_supersedes_its_predecessor_in_one_transaction(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            first = _opened(store)
            digest = _seal(store, run_id="run-2", attempt_number=1, tree_sha="1" * 40)
            successor = store.open_successor_publication_job(
                intent=_intent(digest, run_id="run-2", tree_sha="1" * 40), now=_LATER
            )
            predecessor = store.get_publication_job(first.job_id)

        assert successor.state == PENDING
        assert successor.predecessor_job_id == first.job_id
        assert predecessor is not None
        assert predecessor.state == SUPERSEDED
        assert predecessor.superseded_by == successor.job_id
        assert not predecessor.active

    def test_a_terminal_lineage_admits_a_plain_new_job(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            first = _opened(store)
            store.mark_publication_state(
                job_id=first.job_id,
                state=ABANDONED,
                expected_state=PENDING,
                detail="the host gave up",
                now=_LATER,
            )
            digest = _seal(store, run_id="run-2", attempt_number=1)
            second = store.open_publication_job(
                intent=_intent(digest, run_id="run-2"), now=_LATER
            )

        assert second.state == PENDING
        assert second.job_id != first.job_id

    def test_a_successor_with_no_predecessor_is_simply_the_first_job(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            digest = _seal(store)
            job = store.open_successor_publication_job(intent=_intent(digest), now=_NOW)

        assert job.state == PENDING
        assert job.predecessor_job_id is None

    def test_the_active_job_for_a_lineage_is_the_one_not_yet_terminal(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            first = _opened(store)
            assert store.active_publication_job(first.lineage_key) is not None
            store.mark_publication_state(
                job_id=first.job_id,
                state=ABANDONED,
                expected_state=PENDING,
                now=_LATER,
            )
            assert store.active_publication_job(first.lineage_key) is None


class TestTheLifecycleIsTheOnlyStatusMachine:
    """Forward on evidence, ambiguous on doubt, terminal exactly once."""

    def test_a_remote_observation_moves_the_job_to_what_the_remote_shows(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            pushed = store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(kind=REMOTE_BRANCH, ref="agent/issue-7"),
                now=_LATER,
            )
            opened = store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(
                    kind=REMOTE_PULL_REQUEST, ref="https://example.invalid/pr/7"
                ),
                now=_LATER,
            )
            settled = store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(
                    kind=REMOTE_SETTLED, ref="https://example.invalid/pr/7"
                ),
                now=_LATER,
            )

        assert pushed.state == PUSHED
        assert opened.state == OPENED
        assert opened.remote_ref == "https://example.invalid/pr/7"
        assert settled.state == RECONCILED
        assert not settled.active

    def test_an_unobservable_remote_is_ambiguous_rather_than_a_guess(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            store.mark_publication_state(
                job_id=job.job_id,
                state=COMMITTED,
                expected_state=PENDING,
                head_sha=_HEAD,
                now=_NOW,
            )
            unknown = store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(
                    kind=REMOTE_UNKNOWN, detail="the forge did not answer"
                ),
                now=_LATER,
            )

        assert unknown.state == AMBIGUOUS
        assert unknown.resume_state == COMMITTED
        assert unknown.detail == "the forge did not answer"

    def test_ambiguity_resolves_to_what_the_next_observation_proves(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            store.mark_publication_state(
                job_id=job.job_id,
                state=COMMITTED,
                expected_state=PENDING,
                head_sha=_HEAD,
                now=_NOW,
            )
            store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(kind=REMOTE_UNKNOWN),
                now=_LATER,
            )
            resolved = store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(kind=REMOTE_PULL_REQUEST, ref="pr/7"),
                now=_LATER,
            )

        assert resolved.state == OPENED
        assert resolved.resume_state is None

    def test_an_absent_remote_returns_an_ambiguous_job_to_what_it_last_knew(
        self, tmp_path: Path
    ) -> None:
        """ "Nothing there" un-does the doubt; it never invents progress."""
        with _store(tmp_path) as store:
            job = _opened(store)
            store.mark_publication_state(
                job_id=job.job_id,
                state=COMMITTED,
                expected_state=PENDING,
                head_sha=_HEAD,
                now=_NOW,
            )
            store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(kind=REMOTE_UNKNOWN),
                now=_LATER,
            )
            resolved = store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(kind=REMOTE_ABSENT),
                now=_LATER,
            )

        assert resolved.state == COMMITTED
        assert resolved.resume_state is None

    def test_a_job_never_walks_its_lifecycle_backwards(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            store.observe_publication_remote(
                job_id=job.job_id,
                observation=RemoteObservation(kind=REMOTE_PULL_REQUEST, ref="pr/7"),
                now=_LATER,
            )
            with pytest.raises(PublicationJobStateError) as err:
                store.mark_publication_state(
                    job_id=job.job_id,
                    state=COMMITTED,
                    expected_state=OPENED,
                    now=_LATER,
                )

        assert "backwards" in str(err.value)

    def test_a_terminal_job_refuses_every_further_change(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            store.mark_publication_state(
                job_id=job.job_id,
                state=RECONCILED,
                expected_state=PENDING,
                now=_LATER,
            )
            with pytest.raises(PublicationJobStateError):
                store.mark_publication_state(
                    job_id=job.job_id,
                    state=ABANDONED,
                    expected_state=RECONCILED,
                    now=_LATER,
                )
            with pytest.raises(PublicationJobStateError):
                store.record_publication_attempt(
                    job_id=job.job_id, step="push", now=_LATER
                )
            with pytest.raises(PublicationJobStateError):
                store.observe_publication_remote(
                    job_id=job.job_id,
                    observation=RemoteObservation(kind=REMOTE_BRANCH),
                    now=_LATER,
                )

    def test_a_stale_expectation_never_clobbers_a_newer_state(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            job = _opened(store)
            store.mark_publication_state(
                job_id=job.job_id,
                state=COMMITTED,
                expected_state=PENDING,
                head_sha=_HEAD,
                now=_LATER,
            )
            with pytest.raises(PublicationJobStateError) as err:
                store.mark_publication_state(
                    job_id=job.job_id,
                    state=PUSHED,
                    expected_state=PENDING,
                    now=_LATER,
                )
            assert store.get_publication_job(job.job_id).state == COMMITTED

        assert "expected" in str(err.value)

    def test_an_unknown_job_is_refused_rather_than_silently_created(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store, pytest.raises(PublicationJobStateError):
            store.mark_publication_state(
                job_id="pj-nonexistent",
                state=COMMITTED,
                expected_state=PENDING,
                now=_NOW,
            )


class TestLegacyRowsAreDecodedNotRewritten:
    """`awaiting_publication` is read honestly, and stays where it is."""

    def _legacy_run(self, store: SqliteRunStore, **overrides: object) -> None:
        from kew.run_store import RunRecord

        fields: dict[str, object] = {
            "run_id": "legacy-1",
            "issue_num": 7,
            "branch": "agent/issue-7",
            "status": "awaiting_publication",
            "exit_code": 0,
            "started_at": _NOW,
            "error_category": "github_api",
        }
        fields.update(overrides)
        store.save(RunRecord(**fields))  # type: ignore[arg-type]

    def test_a_legacy_row_reads_as_ambiguous_never_as_pending_or_success(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            self._legacy_run(store)
            views = store.legacy_publication_backlog()

        assert len(views) == 1
        assert views[0].state == AMBIGUOUS
        assert views[0].run_id == "legacy-1"
        assert views[0].branch == "agent/issue-7"
        assert "github_api" in views[0].detail

    def test_a_legacy_row_that_named_a_pull_request_reads_as_opened(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            self._legacy_run(store, pr_url="https://example.invalid/pr/7")
            views = store.legacy_publication_backlog()

        assert views[0].state == OPENED
        assert views[0].remote_ref == "https://example.invalid/pr/7"

    def test_decoding_writes_no_publication_job_row(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            self._legacy_run(store)
            store.legacy_publication_backlog()

            assert store.list_publication_jobs() == []
            assert all(
                not view.persisted for view in store.legacy_publication_backlog()
            )

    def test_a_run_that_never_deferred_publication_decodes_to_nothing(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            self._legacy_run(store, run_id="ok-1", status="success")
            self._legacy_run(store, run_id="failed-1", status="failure")

            assert store.legacy_publication_backlog() == []


class TestTheDecoderAndTheLifecycleRefuseNonsense:
    """The pure functions, exercised without a database in the way."""

    def test_a_status_that_never_meant_publication_decodes_to_nothing(self) -> None:
        assert (
            decode_legacy_publication(
                status="success",
                run_id="run-1",
                attempt_number=1,
                branch="agent/issue-7",
                pr_url="https://example.invalid/pr/7",
            )
            is None
        )

    def test_a_state_the_lifecycle_does_not_name_is_refused(self) -> None:
        with pytest.raises(PublicationJobStateError) as err:
            validate_transition(
                current=PENDING, resume_state=None, target="half-published"
            )

        assert "not a publication job state" in str(err.value)

    def test_an_observation_kind_the_forge_cannot_produce_is_refused(self) -> None:
        with pytest.raises(PublicationJobValueError):
            resolve_observation(
                state=PENDING,
                resume_state=None,
                observation=RemoteObservation(kind="probably-fine"),
            )


class TestNothingHereCanPublishYet:
    """Task 36: the selector is closed until E7 installs capacity."""

    def test_the_gate_is_off_in_the_shipped_source(self) -> None:
        assert publication_jobs_active() is False

    def test_the_selector_refuses_to_hand_out_work_while_the_gate_is_shut(
        self, tmp_path: Path
    ) -> None:
        with _store(tmp_path) as store:
            _opened(store)
            with pytest.raises(PublicationJobsInactiveError) as err:
                store.next_publication_job()

        assert "E7" in str(err.value)

    def test_the_selector_hands_out_the_oldest_active_job_once_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kew.publication_job.PUBLICATION_JOBS_ACTIVE", True)
        with _store(tmp_path) as store:
            job = _opened(store)
            selected = store.next_publication_job()
            store.mark_publication_state(
                job_id=job.job_id,
                state=RECONCILED,
                expected_state=PENDING,
                now=_LATER,
            )
            drained = store.next_publication_job()

        assert selected is not None and selected.job_id == job.job_id
        assert drained is None

    @pytest.mark.policy
    def test_no_dispatch_or_product_verb_names_the_publication_job_modules(
        self,
    ) -> None:
        """Source inspection: E5's persistence has no caller to reach it.

        I14 cut the review-capacity callers into this door and narrowed what
        the guard actually guards. Naming the lifecycle is no longer the
        dangerous thing — the capacity transaction, the reconciliation and the
        deferral/finalization writers all have to name it. Reaching the single
        *effect* gate is. So the check below is on
        :meth:`next_publication_job`, the one method that would hand a job to a
        worker, and it still has no caller anywhere in shipped source.
        """
        src = Path(__file__).resolve().parents[1] / "src" / "kew"
        allowed = {
            src / "publication_job.py",
            src / "run_store_publication_jobs.py",
            src / "run_store.py",
            # I09A's worker acts on these jobs and is itself unreachable — its
            # own suite asserts nothing in src names IT, which is what keeps
            # this allowance from widening the door it is cut into.
            src / "publication_worker.py",
            # I14's atomic capacity transaction and its two ends: the
            # reconciliation that reads the forge, the audited verbs that
            # reserve and release, and the dispatch writers that were cut over
            # from the overloaded run status. None of them publishes.
            src / "outcomes.py",
            src / "service" / "verbs.py",
            src / "dispatch_core.py",
            # I15's driver backpressure READS the capacity facts (the persisted
            # observation, the effective policy, the occupancy count) so a
            # supervised driver stops launching fresh work while people are the
            # constraint. It reserves nothing, publishes nothing, and — like
            # every entry above — never reaches the effect gate below.
            src / "graph" / "driver" / "facts.py",
            # I19's canonical work projection READS the same capacity facts
            # for a customer surface: whether a preserved artifact is held for
            # a person, and whether the configured policy is enforcing yet (it
            # is not, pre-broker, and the projection says so). Like the driver
            # above it reserves nothing, publishes nothing, and never reaches
            # the effect gate below.
            src / "service" / "work.py",
            # kew rename-repo (#3527) rewrites ``publication_job.delivery_repo``
            # from a renamed repository's old slug to its new one, in the same
            # transaction as the rest of the ledger. It changes no job's state,
            # publishes nothing, and never reaches the effect gate below.
            src / "run_store_rename.py",
        }
        naming = {
            path
            for path in src.rglob("*.py")
            if path not in allowed
            and ("publication_job" in path.read_text(encoding="utf-8"))
        }
        # The gate itself, and the worker that is the only thing it would ever
        # hand a job to — and which nothing in src names, per its own suite.
        gate_owners = {
            src / "run_store_publication_jobs.py",
            src / "publication_worker.py",
        }
        selecting = {
            path
            for path in src.rglob("*.py")
            if path not in gate_owners
            and "next_publication_job" in path.read_text(encoding="utf-8")
        }

        assert naming == set()
        assert selecting == set()


class TestConcurrentWritersSerialize:
    """Task 36: two hosts, one lineage, one truth."""

    def test_racing_creators_leave_exactly_one_active_job(self, tmp_path: Path) -> None:
        db = str(tmp_path / "dispatch.db")
        with SqliteRunStore(db) as setup:
            for index in range(6):
                _seal(setup, run_id=f"run-{index}", attempt_number=1)

        outcomes: list[object] = []
        barrier = threading.Barrier(6)

        def _create(index: int) -> None:
            with SqliteRunStore(db, check_same_thread=False) as store:
                digest = store.get_artifact_receipt(
                    run_id=f"run-{index}", attempt_number=1
                ).receipt_digest
                barrier.wait(timeout=10)
                try:
                    outcomes.append(
                        store.open_publication_job(
                            intent=_intent(digest, run_id=f"run-{index}"), now=_NOW
                        )
                    )
                except (PublicationJobConflictError, sqlite3.IntegrityError) as exc:
                    outcomes.append(exc)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        created = [item for item in outcomes if not isinstance(item, Exception)]
        assert len(created) == 1, outcomes
        with SqliteRunStore(db) as store:
            assert len(store.list_publication_jobs()) == 1

    def test_racing_transition_writers_admit_exactly_one_winner(
        self, tmp_path: Path
    ) -> None:
        db = str(tmp_path / "dispatch.db")
        with SqliteRunStore(db) as setup:
            job = _opened(setup)

        outcomes: list[object] = []
        barrier = threading.Barrier(6)

        def _advance() -> None:
            with SqliteRunStore(db, check_same_thread=False) as store:
                barrier.wait(timeout=10)
                try:
                    outcomes.append(
                        store.mark_publication_state(
                            job_id=job.job_id,
                            state=COMMITTED,
                            expected_state=PENDING,
                            head_sha=_HEAD,
                            now=_LATER,
                        )
                    )
                except PublicationJobStateError as exc:
                    outcomes.append(exc)

        threads = [threading.Thread(target=_advance) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        winners = [item for item in outcomes if not isinstance(item, Exception)]
        assert len(winners) == 1, outcomes
        with SqliteRunStore(db) as store:
            reread = store.get_publication_job(job.job_id)
        assert reread is not None and reread.state == COMMITTED
