"""I09A/I09B: the inactive worker materializes, pushes and reconciles once.

The question this node answers is the one a host has after a crash: *is the
commit I would make now the same commit I may already have made?* It is
answered by construction — every field of the commit comes off the persisted
job, so recomputing it is the same act as remembering it — and these tests
crash the worker at each seam to prove it.

Three properties are asserted rather than assumed.

* **Repeatability.** Crashing before object creation, after object creation but
  before the state write, and a cold restart against a reopened database all
  converge on one SHA.
* **Refusal over invention.** A tree the canonical repository does not hold, a
  base it was not provisioned at, a successor whose parent is missing, and a
  job whose sealed receipt disagrees with it are refusals. None of them fall
  back to a commit built from whatever was to hand.
* **Nothing here can publish.** The selector remains shut, the worker has no
  dispatch or product caller, and it runs no git of its own — every git call
  goes through the canonical repository I07 provisioned, never an agent
  checkout and never the agent's git metadata.

I09B extends the same three properties past the commit, to the two effects that
leave the host. A push and a pull request cannot be recomputed the way a commit
can, so their equivalent of repeatability is *observation*: every repeat looks
at the remote first, binds what is compatible with the lineage, holds what is
not, and resolves a lost response by looking rather than by trying again.
"""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

from kew import subprocess_utils
from kew.artifact_receipt import ReceiptAttestation
from kew.publication_apply import apply_to_tree
from kew.publication_credentials import (
    CredentialPublicationRefused,
    require_normalized_job_credentials,
)
from kew.publication_filter import ManifestEntry
from kew.publication_job import (
    ABANDONED,
    AMBIGUOUS,
    COMMITTED,
    OPENED,
    PENDING,
    PR_CLOSED,
    PUSHED,
    RECONCILED,
    REMOTE_CONFLICT,
    REMOTE_UNKNOWN,
    STEP_COMMIT,
    STEP_PR_CREATE,
    STEP_PUSH,
    ObservedBranch,
    ObservedPullRequest,
    PublicationIntent,
    PublicationJob,
    PublicationJobsInactiveError,
    PublicationRemoteConflictError,
    PullRequestIntent,
    PushIntent,
    RemoteObservation,
    commit_intent_for,
    publication_jobs_active,
)
from kew.publication_repository import (
    VERIFICATION_EXACT_COMMIT,
    CanonicalRepository,
    commit_tree,
    provision,
    read_commit,
)
from kew.publication_worker import (
    COMMIT_ATTESTATION,
    PULL_REQUEST_ATTESTATION,
    PUSH_ATTESTATION,
    RECONCILE_ATTESTATION,
    PublicationWorkerError,
    finalize_publication,
    materialize_commit,
    materialize_next,
    open_publication_pull_request,
    push_publication,
    reconcile_next,
    reconcile_publication,
)
from kew.run_store import ArtifactLineage, RunRecord, SqliteRunStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="materialization is git"
)

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
_PACKAGE = "sha256:" + "a" * 64
_WORKER_SOURCE = Path(__file__).resolve().parents[1] / "src" / "kew"


@pytest.fixture(autouse=True)
def _git_on_the_allowlist(monkeypatch: pytest.MonkeyPatch):
    resolved = shutil.which("git")
    assert resolved is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


@pytest.fixture(autouse=True)
def _held_admitted_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold a placeholder for the ``env:`` credential the fixture runs record.

    The scan must hold every credential a run was admitted with (round 12).
    The conftest already points the scan at a placeholder Codex login.
    """
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "kew-placeholder-synthetic-key-0123")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _canonical(tmp_path: Path) -> CanonicalRepository:
    """A bare host-owned repository holding exactly one base commit."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", ".")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "keep.txt").write_text("keep\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    base = _git(seed, "rev-parse", "HEAD")

    dest = tmp_path / "canonical.git"
    dest.mkdir(mode=0o700)
    _git(dest.parent, "init", "-q", "--bare", str(dest))
    _git(seed, "push", "-q", str(dest), f"{base}:refs/heads/main")
    return CanonicalRepository(path=dest, base_sha=base)


def _promote(repo: CanonicalRepository, tmp_path: Path, content: bytes) -> str:
    """The tree a preserved package became, written into *repo* by I07's applier."""
    return apply_to_tree(
        repo.path,
        base_sha=repo.base_sha,
        approved=(ManifestEntry(path="added.txt", mode="100644", sha256="0" * 64),),
        deletions=(),
        content=lambda _entry: content,
        index_file=tmp_path / f"promote-{len(content)}.index",
    )


def _seal(
    store: SqliteRunStore,
    *,
    base_sha: str,
    tree_sha: str,
    run_id: str = "run-1",
    attempt_number: int = 1,
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
            verification="exact_tree",
            locator="packages/" + "a" * 64,
        ),
        sealed_at=_NOW,
    )
    return receipt.receipt_digest


def _intent(
    digest: str, *, base_sha: str, tree_sha: str, **overrides: object
) -> PublicationIntent:
    fields: dict[str, object] = {
        "source_kind": "github",
        "source_id": "acme/widgets",
        "run_id": "run-1",
        "attempt_number": 1,
        "branch": "agent/issue-7",
        "base_sha": base_sha,
        "tree_sha": tree_sha,
        "receipt_digest": digest,
        "message": "feat(#7): the change the agent made",
    }
    fields.update(overrides)
    return PublicationIntent(**fields)  # type: ignore[arg-type]


def _native_profile(run_id: str):
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )

    return compose_realized_profile(
        run_id=run_id,
        attempt_number=1,
        route=compose_route_profile(
            runner="claude-code", requested_model="claude-opus-5"
        ),
        boundary=RealizedLaunchBoundary(
            containment=Containment.NATIVE,
            auth_path="cli-login",
        ),
    )


def _stored_native_profile(run_id: str) -> SimpleNamespace:
    profile = _native_profile(run_id)
    return SimpleNamespace(
        attempt_number=1,
        profile_json=profile.to_json(),
        fingerprint=profile.fingerprint,
        containment="native",
        auth_path="cli-login",
    )


class _Scene:
    """One canonical repository, one sealed receipt, one pending job."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.repo = _canonical(tmp_path)
        self.tree = _promote(self.repo, tmp_path, b"added\n")
        self.db = str(tmp_path / "dispatch.db")
        self.store = SqliteRunStore(self.db)
        self.store.save(
            RunRecord(
                run_id="run-1",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="awaiting_publication",
                exit_code=0,
                started_at=_NOW,
                runner_name="claude-code",
                model="claude-opus-5",
            )
        )
        digest = _seal(self.store, base_sha=self.repo.base_sha, tree_sha=self.tree)
        self.job = self.store.open_publication_job(
            intent=_intent(digest, base_sha=self.repo.base_sha, tree_sha=self.tree),
            now=_NOW,
        )

    def reread(self) -> PublicationJob:
        job = self.store.get_publication_job(self.job.job_id)
        assert job is not None
        return job

    def attested(
        self, kind: str, *, attempt_number: int = 1
    ) -> list[ReceiptAttestation]:
        return [
            a
            for a in self.store.artifact_receipt_attestations(
                run_id="run-1", attempt_number=attempt_number
            )
            if a.kind == kind
        ]

    def commits(self, *, attempt_number: int = 1) -> list[ReceiptAttestation]:
        return self.attested(COMMIT_ATTESTATION, attempt_number=attempt_number)


@pytest.fixture
def scene(tmp_path: Path):
    built = _Scene(tmp_path)
    yield built
    built.store.close()


class TestTheCommitIsTheOneThePersistedJobNames:
    def test_it_creates_the_commit_the_job_named(self, scene: _Scene) -> None:
        made = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        facts = read_commit(scene.repo, made.head_sha)
        assert facts.tree_sha == scene.tree
        assert facts.parents == (scene.repo.base_sha,)
        assert made.verification == VERIFICATION_EXACT_COMMIT

    def test_the_job_advances_to_committed_and_remembers_the_head(
        self, scene: _Scene
    ) -> None:
        made = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        reread = scene.reread()
        assert reread.state == COMMITTED
        assert reread.head_sha == made.head_sha

    def test_the_intent_is_recorded_before_the_object_is_created(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash between the two leaves a row that says an attempt was made
        and nothing that claims it succeeded."""
        seen: list[str | None] = []

        def _watch(repo: CanonicalRepository, intent: object) -> str:
            seen.append(scene.reread().last_step)
            return commit_tree(repo, intent)  # type: ignore[arg-type]

        monkeypatch.setattr("kew.publication_worker.commit_tree", _watch)
        materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        assert seen and seen[0] == STEP_COMMIT

    def test_the_commit_is_attested_against_the_sealed_receipt(
        self, scene: _Scene
    ) -> None:
        made = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        commits = scene.commits()
        assert len(commits) == 1
        assert commits[0].payload["head_sha"] == made.head_sha
        assert commits[0].payload["verification"] == VERIFICATION_EXACT_COMMIT
        assert commits[0].terminal is False


class TestTheSameSHAComesBackAfterACrash:
    def test_a_crash_before_the_object_is_created_retries_into_it(
        self, scene: _Scene
    ) -> None:
        def _explode(*args: object, **kwargs: object) -> str:
            raise OSError("the host died mid-commit")

        # A nested context, so undoing this patch leaves the autouse allowlist
        # entry — which the retry below still needs — in place.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("kew.publication_worker.commit_tree", _explode)
            with pytest.raises(OSError, match="died"):
                materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        stalled = scene.reread()
        assert stalled.state == PENDING and stalled.attempt_count == 1

        made = materialize_commit(scene.store, scene.repo, job=stalled, now=_NOW)
        assert read_commit(scene.repo, made.head_sha).tree_sha == scene.tree

    def test_a_crash_after_the_object_but_before_the_state_write_converges(
        self, scene: _Scene
    ) -> None:
        """The commit object exists and the row still says pending. The retry
        must find the SAME SHA, not create a second commit beside it."""
        created: list[str] = []

        def _die_after(repo: CanonicalRepository, intent: object) -> str:
            created.append(commit_tree(repo, intent))  # type: ignore[arg-type]
            raise OSError("the host died after commit-tree")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("kew.publication_worker.commit_tree", _die_after)
            with pytest.raises(OSError, match="died"):
                materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        made = materialize_commit(scene.store, scene.repo, job=scene.reread(), now=_NOW)

        assert made.head_sha == created[0]
        assert scene.reread().state == COMMITTED

    def test_a_cold_restart_recreates_the_same_sha(self, scene: _Scene) -> None:
        first = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
        scene.store.close()

        scene.store = SqliteRunStore(scene.db)
        again = materialize_commit(
            scene.store, scene.repo, job=scene.reread(), now=_NOW
        )

        assert again.head_sha == first.head_sha
        # Re-materializing an already-committed job appends no second fact.
        assert len(scene.commits()) == 1

    def test_a_recorded_head_that_is_not_the_one_the_job_names_is_refused(
        self, scene: _Scene
    ) -> None:
        """Incompatible state, not something to quietly overwrite."""
        materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
        forged = replace(scene.reread(), head_sha="d" * 40)

        with pytest.raises(PublicationWorkerError, match="already recorded"):
            materialize_commit(scene.store, scene.repo, job=forged, now=_NOW)


class TestAStateItCannotCommitIsRefused:
    def test_a_base_the_repository_was_not_provisioned_at_is_refused(
        self, scene: _Scene
    ) -> None:
        elsewhere = CanonicalRepository(path=scene.repo.path, base_sha="b" * 40)

        with pytest.raises(PublicationWorkerError, match="trusted base"):
            materialize_commit(scene.store, elsewhere, job=scene.job, now=_LATER)

    def test_a_tree_the_repository_does_not_hold_is_refused(
        self, scene: _Scene
    ) -> None:
        with pytest.raises(PublicationWorkerError, match="not in the canonical"):
            materialize_commit(
                scene.store,
                scene.repo,
                job=replace(scene.job, tree_sha="c" * 40),
                now=_LATER,
            )

    def test_a_job_whose_receipt_was_never_sealed_is_refused(
        self, scene: _Scene
    ) -> None:
        orphan = replace(scene.job, run_id="run-missing")

        with pytest.raises(PublicationWorkerError, match="sealed no artifact receipt"):
            materialize_commit(scene.store, scene.repo, job=orphan, now=_LATER)

    def test_a_job_that_disagrees_with_its_sealed_receipt_is_refused(
        self, scene: _Scene
    ) -> None:
        other = _promote(scene.repo, scene.tmp_path, b"a different package\n")

        with pytest.raises(PublicationWorkerError, match="sealed"):
            materialize_commit(
                scene.store,
                scene.repo,
                job=replace(scene.job, tree_sha=other),
                now=_LATER,
            )

    @pytest.mark.parametrize("state", [RECONCILED, ABANDONED])
    def test_a_settled_job_commits_nothing(self, scene: _Scene, state: str) -> None:
        scene.store.mark_publication_state(
            job_id=scene.job.job_id,
            state=state,
            expected_state=PENDING,
            now=_LATER,
        )

        with pytest.raises(PublicationWorkerError, match="settled"):
            materialize_commit(scene.store, scene.repo, job=scene.reread(), now=_NOW)


class TestASuccessorCommitsOntoWhatItsPredecessorLeft:
    def _successor(self, scene: _Scene, *, materialize: bool = True) -> PublicationJob:
        if materialize:
            materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
        tree = _promote(scene.repo, scene.tmp_path, b"the successor package\n")
        digest = _seal(
            scene.store,
            base_sha=scene.repo.base_sha,
            tree_sha=tree,
            attempt_number=2,
        )
        scene.store.record_route_profile(_native_profile(scene.job.run_id))
        return scene.store.open_successor_publication_job(
            intent=_intent(
                digest,
                base_sha=scene.repo.base_sha,
                tree_sha=tree,
                attempt_number=2,
            ),
            now=_LATER,
        )

    def test_the_parent_is_the_head_the_predecessor_recorded(
        self, scene: _Scene
    ) -> None:
        successor = self._successor(scene)
        predecessor_head = scene.reread().head_sha

        made = materialize_commit(scene.store, scene.repo, job=successor, now=_LATER)

        assert read_commit(scene.repo, made.head_sha).parents == (predecessor_head,)

    def test_a_predecessor_head_the_repository_lost_is_refused(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parent kew cannot see is a mismatch, never a reparent onto base."""
        successor = self._successor(scene)
        lost = scene.reread().head_sha
        monkeypatch.setattr(
            CanonicalRepository,
            "holds_commit",
            lambda self, sha: sha != lost,
        )

        with pytest.raises(PublicationWorkerError, match="parent"):
            materialize_commit(scene.store, scene.repo, job=successor, now=_LATER)

    def test_a_predecessor_that_never_committed_leaves_the_base_as_parent(
        self, scene: _Scene
    ) -> None:
        """Nothing was published under the old SHA, so there is nothing to
        build on — and inventing a parent would be worse than using the base."""
        successor = self._successor(scene, materialize=False)

        made = materialize_commit(scene.store, scene.repo, job=successor, now=_LATER)

        assert read_commit(scene.repo, made.head_sha).parents == (scene.repo.base_sha,)


class TestTheEvidenceAndTheRowCannotDisagree:
    """Task 39: the attestation is written before the row it corroborates.

    The commit object is real the moment ``commit-tree`` returns, so evidence
    naming it is honest even if the host dies immediately after. A row claiming
    ``committed`` with nothing behind it would not be, which is why the order is
    what it is — and why the attestation has to be idempotent against the head
    it names, or the retry would leave two facts about one commit.
    """

    def test_a_crash_between_the_evidence_and_the_row_leaves_one_fact(
        self, scene: _Scene
    ) -> None:
        def _explode(**kwargs: object) -> None:
            raise OSError("the host died before the row moved")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(scene.store, "mark_publication_state", _explode)
            with pytest.raises(OSError, match="died"):
                materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        assert scene.reread().state == PENDING
        assert len(scene.commits()) == 1

        made = materialize_commit(scene.store, scene.repo, job=scene.reread(), now=_NOW)

        assert scene.reread().head_sha == made.head_sha
        assert len(scene.commits()) == 1

    def test_a_repository_that_does_not_name_one_commit_is_refused(
        self, scene: _Scene
    ) -> None:
        """The verification is the claim, so it has to be able to fail."""
        shas = iter(("a" * 40, "b" * 40))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "kew.publication_worker.commit_tree",
                lambda *args, **kwargs: next(shas),
            )
            with pytest.raises(PublicationWorkerError, match="does not name one"):
                materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        assert scene.reread().state == PENDING
        assert scene.commits() == []


class TestAgentGitMetadataNeverReachesTheCommit:
    def test_a_poisoned_environment_does_not_move_the_sha(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clean = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        for name, value in (
            ("GIT_DIR", str(scene.tmp_path / "seed" / ".git")),
            ("GIT_AUTHOR_NAME", "agent"),
            ("GIT_AUTHOR_DATE", "@1 +0000"),
            ("GIT_COMMITTER_DATE", "@1 +0000"),
            ("GIT_CONFIG_COUNT", "1"),
            ("GIT_CONFIG_KEY_0", "commit.gpgsign"),
            ("GIT_CONFIG_VALUE_0", "true"),
        ):
            monkeypatch.setenv(name, value)

        again = materialize_commit(
            scene.store, scene.repo, job=scene.reread(), now=_NOW
        )

        assert again.head_sha == clean.head_sha

    def test_a_poisoned_global_git_config_does_not_move_the_sha(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 39: the host's own ``~/.gitconfig`` is metadata too, and a
        ``commit.gpgsign`` in it would make every commit unrepeatable."""
        clean = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        home = scene.tmp_path / "poisoned-home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[user]\n\tname = agent\n\temail = agent@example.invalid\n"
            "[commit]\n\tgpgsign = true\n"
        )
        monkeypatch.setenv("HOME", str(home))

        again = materialize_commit(
            scene.store, scene.repo, job=scene.reread(), now=_NOW
        )

        assert again.head_sha == clean.head_sha

    def test_the_worker_runs_no_git_of_its_own(self) -> None:
        """Source inspection: every git call goes through the canonical
        repository, so there is no second place an agent path could be passed."""
        source = (_WORKER_SOURCE / "publication_worker.py").read_text(encoding="utf-8")

        assert "subprocess" not in source
        assert '"git"' not in source
        assert "from kew.publication_repository import" in source

    def test_the_worker_is_handed_a_repository_and_never_a_path(self) -> None:
        """Task 39: the type IS the guard. A caller who meant to hand over the
        agent's directory cannot, because only I07's provisioner makes one of
        these and it refuses a source the host cannot prove it owns."""
        for entry in (materialize_commit, materialize_next):
            second = list(inspect.signature(entry).parameters.values())[1]
            assert second.annotation == "CanonicalRepository"


class TestNothingCanPublishYet:
    def test_the_selector_is_still_shut(self, scene: _Scene) -> None:
        with pytest.raises(PublicationJobsInactiveError):
            materialize_next(scene.store, scene.repo, now=_LATER)

    @pytest.mark.policy
    def test_no_dispatch_or_product_verb_names_the_worker(self) -> None:
        naming = {
            path
            for path in _WORKER_SOURCE.rglob("*.py")
            if path.name != "publication_worker.py"
            and "publication_worker" in path.read_text(encoding="utf-8")
        }

        assert naming == set()

    def test_the_gate_is_shut_in_shipped_source(self) -> None:
        """Task 39: E7 owns flipping this, together with the capacity
        transaction that makes starting a publication safe."""
        assert publication_jobs_active() is False

    def test_the_selector_is_the_only_door_the_worker_opens(self) -> None:
        """Source inspection: nothing here reaches around the gated selector to
        a list, a scan or a lineage lookup that would hand out work anyway."""
        source = (_WORKER_SOURCE / "publication_worker.py").read_text(encoding="utf-8")

        assert "next_publication_job" in source
        for around in (
            "list_publication_jobs",
            "active_publication_job",
            "publication_job_for_attempt",
            "legacy_publication_backlog",
        ):
            assert around not in source

    def test_the_intent_a_job_names_is_the_intent_that_was_committed(
        self, scene: _Scene
    ) -> None:
        """The worker adds nothing of its own between the row and the commit."""
        made = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
        intent = commit_intent_for(scene.job)

        facts = read_commit(scene.repo, made.head_sha)
        assert (facts.tree_sha, facts.parents) == (
            intent.tree_sha,
            (intent.parent_sha,),
        )


class TestTheSHASurvivesLosingEverythingButTheRows:
    """Task 39: the commit is named by the database, not by a directory.

    The cold-restart test above reopens the database and keeps the repository.
    This one throws the repository away too — reprovisions it from the source,
    re-promotes the package into it — and asks for the same SHA back. If any
    part of the commit came from the workspace rather than from a persisted
    row, this is where it shows.
    """

    def _reprovisioned(self, scene: _Scene) -> CanonicalRepository:
        mirror = scene.tmp_path / "mirror.git"
        _git(scene.tmp_path, "init", "-q", "--bare", str(mirror))
        _git(
            scene.tmp_path / "seed",
            "push",
            "-q",
            str(mirror),
            f"{scene.repo.base_sha}:refs/heads/main",
        )
        fresh = provision(
            scene.tmp_path / "second.git",
            source=str(mirror),
            base_sha=scene.repo.base_sha,
        )
        scratch = scene.tmp_path / "second-scratch"
        scratch.mkdir()
        _promote(fresh, scratch, b"added\n")
        return fresh

    def test_a_repository_rebuilt_from_scratch_names_the_same_commit(
        self, scene: _Scene
    ) -> None:
        first = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
        fresh = self._reprovisioned(scene)

        again = materialize_commit(scene.store, fresh, job=scene.reread(), now=_NOW)

        assert again.head_sha == first.head_sha
        assert read_commit(fresh, again.head_sha).tree_sha == scene.tree

    def test_a_job_that_has_moved_past_the_commit_is_confirmed_not_remade(
        self, scene: _Scene
    ) -> None:
        """A pushed job still has to be able to answer "which SHA?" — and must
        not be walked backwards into ``committed`` to do it."""
        first = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
        scene.store.mark_publication_state(
            job_id=scene.job.job_id,
            state=PUSHED,
            expected_state=COMMITTED,
            now=_LATER,
        )

        again = materialize_commit(
            scene.store, scene.repo, job=scene.reread(), now=_NOW
        )

        assert again.head_sha == first.head_sha
        assert scene.reread().state == PUSHED
        assert len(scene.commits()) == 1


_BASE_BRANCH = "main"
_LEAKY_TOKEN = "ghp_" + "z" * 36  # nosec B105 — a fake, to prove it is redacted


class _Forge:
    """A fake forge: one branch table, one pull-request table, and faults.

    It enforces the lease every push carries, because a fake that accepted any
    push would let a compare-and-set defect through unnoticed. The pull request
    it reports follows the branch, which is what a real forge does — a PR's head
    is the branch's head, not the sha it was opened at.
    """

    def __init__(self) -> None:
        self.branches: dict[str, str] = {}
        self.pulls: list[ObservedPullRequest] = []
        self.pushes: list[PushIntent] = []
        self.created: list[PullRequestIntent] = []
        self.reads = 0
        self.unreadable: Exception | None = None
        self.refuse_push: Exception | None = None
        self.lose_push = False
        self.lose_create = False
        self._number = 6

    def observe_branch(self, branch: str) -> ObservedBranch:
        self.reads += 1
        if self.unreadable is not None:
            raise self.unreadable
        return ObservedBranch(name=branch, head_sha=self.branches.get(branch))

    def observe_pull_request(self, branch: str) -> ObservedPullRequest | None:
        self.reads += 1
        if self.unreadable is not None:
            raise self.unreadable
        found = [pull for pull in self.pulls if pull.head_branch == branch]
        if not found:
            return None
        return replace(found[-1], head_sha=self.branches.get(branch))

    def push_branch(self, intent: PushIntent) -> None:
        self.pushes.append(intent)
        if self.refuse_push is not None:
            raise self.refuse_push
        if self.branches.get(intent.branch) != intent.expected_head:
            raise RuntimeError(f"stale lease for {intent.branch}")
        self.branches[intent.branch] = intent.head_sha
        if self.lose_push:
            raise RuntimeError("the connection dropped after the push landed")

    def create_pull_request(self, intent: PullRequestIntent) -> ObservedPullRequest:
        self.created.append(intent)
        self._number += 1
        opened = ObservedPullRequest(
            number=self._number,
            url=f"https://forge.invalid/pull/{self._number}",
            head_branch=intent.branch,
            base_branch=intent.base_branch,
            head_sha=self.branches.get(intent.branch),
            body=intent.body,
        )
        self.pulls.append(opened)
        if self.lose_create:
            raise RuntimeError("the connection dropped after the pull request opened")
        return opened


def _scan_finds_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the byte scan report one raw match; recording and gating stay real."""
    from kew import publication_credentials
    from kew.publication_scan import OutputScan, OutputScanTarget

    def scan_output(target, values):
        assert isinstance(target, OutputScanTarget)
        assert isinstance(values, tuple)
        return OutputScan(verdict="found", files_scanned=1, matches={"value_raw": 1})

    monkeypatch.setattr(publication_credentials, "scan_output", scan_output)


def _absent(tmp_path: Path) -> CanonicalRepository:
    """A canonical repository handle that holds no objects at all."""
    return CanonicalRepository(path=tmp_path / "absent.git", base_sha="a" * 40)


def _scan_records(scene: _Scene, head_sha: str | None) -> list:
    return [
        event
        for event in scene.store.list_audit_events(
            event="publication_output_scan", run_id=scene.job.run_id
        )
        if event.payload.get("head_sha") == head_sha
    ]


def _committed(scene: _Scene) -> PublicationJob:
    materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)
    return scene.reread()


def _mark_producer_credential_exposed(scene: _Scene) -> None:
    """Attach contained credential exposure to the job's producer attempt."""
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )
    from kew.run_store import RunRecord

    scene.store.save(
        RunRecord(
            run_id=scene.job.run_id,
            issue_num=7,
            item_id="7",
            branch=scene.job.branch,
            status="awaiting_publication",
            exit_code=0,
            started_at=_NOW,
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )
    scene.store.record_route_profile(
        compose_realized_profile(
            run_id=scene.job.run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="kew-agent:latest",
                image_id="sha256:" + "b" * 64,
                auth_path="env:SYNTHETIC_PROVIDER_KEY",
            ),
        )
    )


def _successor_with_predecessor(
    tmp_path: Path, *, unknown_predecessor: bool = False
) -> tuple[SqliteRunStore, PublicationJob]:
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )
    from kew.run_store import RunRecord

    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    base_sha = "a" * 40
    first_tree = "b" * 40
    first_digest = _seal(store, base_sha=base_sha, tree_sha=first_tree)
    predecessor = store.open_publication_job(
        intent=_intent(first_digest, base_sha=base_sha, tree_sha=first_tree),
        now=_NOW,
    )
    store.mark_publication_state(
        job_id=predecessor.job_id,
        state=COMMITTED,
        expected_state=PENDING,
        head_sha="c" * 40,
        now=_NOW,
    )
    second_tree = "d" * 40
    second_digest = _seal(
        store,
        base_sha=base_sha,
        tree_sha=second_tree,
        run_id="run-2",
    )
    successor = store.open_successor_publication_job(
        intent=_intent(
            second_digest,
            base_sha=base_sha,
            tree_sha=second_tree,
            run_id="run-2",
        ),
        now=_LATER,
    )
    predecessor_containment = (
        Containment.UNKNOWN if unknown_predecessor else Containment.CONTAINED
    )
    predecessor_auth = (
        "cli-login" if unknown_predecessor else "env:SYNTHETIC_PROVIDER_KEY"
    )
    for run_id, containment, auth_path in (
        ("run-1", predecessor_containment, predecessor_auth),
        ("run-2", Containment.NATIVE, "cli-login"),
    ):
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=7,
                item_id="7",
                branch=successor.branch,
                status="awaiting_publication",
                exit_code=0,
                started_at=_NOW,
                runner_name="claude-code",
                model="claude-opus-5",
            )
        )
        store.record_route_profile(
            compose_realized_profile(
                run_id=run_id,
                attempt_number=1,
                route=compose_route_profile(
                    runner="claude-code", requested_model="claude-opus-5"
                ),
                boundary=RealizedLaunchBoundary(
                    containment=containment,
                    image_ref=(
                        "kew-agent:latest"
                        if containment is Containment.CONTAINED
                        else None
                    ),
                    image_id=(
                        "sha256:" + "e" * 64
                        if containment is Containment.CONTAINED
                        else None
                    ),
                    auth_path=auth_path,
                ),
            )
        )
    if unknown_predecessor:
        store._conn.execute(  # noqa: SLF001
            "UPDATE run_route_profile SET containment = 'native',"
            " auth_path = 'cli-login' WHERE run_id = 'run-1'"
        )
        store._conn.commit()  # noqa: SLF001
    assert successor.predecessor_job_id == predecessor.job_id
    return store, successor


def _requeue_unauthenticated_continuation(scene: _Scene) -> None:
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )

    scene.store.save(
        RunRecord(
            run_id="producer",
            issue_num=7,
            item_id="7",
            branch=scene.job.branch,
            status="awaiting_publication",
            exit_code=0,
            started_at=_NOW,
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )
    # The contained producer recorded no authentication evidence: incomplete
    # lineage evidence, refused whatever the output scan says.
    for run_id, containment, auth_path in (
        ("producer", Containment.CONTAINED, None),
        (scene.job.run_id, Containment.NATIVE, "cli-login"),
    ):
        scene.store.record_route_profile(
            compose_realized_profile(
                run_id=run_id,
                attempt_number=1,
                route=compose_route_profile(
                    runner="claude-code", requested_model="claude-opus-5"
                ),
                boundary=RealizedLaunchBoundary(
                    containment=containment,
                    image_ref=(
                        "kew-agent:latest"
                        if containment is Containment.CONTAINED
                        else None
                    ),
                    image_id=(
                        "sha256:" + "e" * 64
                        if containment is Containment.CONTAINED
                        else None
                    ),
                    auth_path=auth_path,
                ),
            )
        )
    preserved = scene.tmp_path / "preserved"
    scene.store.record_continuation(
        "epic:7",
        "7",
        "producer",
        scene.job.branch,
        preserved,
        checkout_generation=fixture_checkout_generation(preserved),
    )
    assert (
        scene.store.claim_continuation_under_cap(
            "epic:7",
            "7",
            "producer",
            scene.job.branch,
            preserved,
            scene.job.run_id,
            0,
            claim_evidence=fixture_claim_evidence(
                scene.store,
                "epic:7",
                "7",
                "producer",
                scene.job.branch,
                preserved,
                scene.job.run_id,
            ),
        )
        == "claimed"
    )
    record = scene.store.get_continuation(
        "epic:7", "7", "producer", scene.job.branch, preserved
    )
    assert record is not None
    assert scene.store.mark_continuation_awaiting(
        "epic:7",
        "7",
        "producer",
        scene.job.branch,
        preserved,
        record.generation,
        scene.job.run_id,
    )


class TestCredentialExposedJobsHaveNoPublicationEffects:
    def test_exposed_output_scanned_clean_is_committed_with_its_scan(
        self, scene: _Scene
    ) -> None:
        _mark_producer_credential_exposed(scene)

        made = materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        (scan,) = scene.store.list_audit_events(
            event="publication_output_scan", run_id=scene.job.run_id
        )
        assert scan.payload["head_sha"] == made.head_sha
        assert scan.payload["verdict"] == "clean"

    def test_credential_in_output_refuses_before_the_commit_is_recorded(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mark_producer_credential_exposed(scene)
        _scan_finds_a_credential(monkeypatch)

        with pytest.raises(CredentialPublicationRefused) as raised:
            materialize_commit(scene.store, scene.repo, job=scene.job, now=_LATER)

        assert raised.value.reason == "credential_in_output"
        assert scene.commits() == []
        assert scene.reread().head_sha is None

    def test_a_head_without_its_own_clean_scan_is_never_pushed(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The caller's job and the row agree on a head that has no scan record.
        committed = self._materialized_before_the_scan(scene, monkeypatch)
        forge = _Forge()

        with pytest.raises(CredentialPublicationRefused) as raised:
            push_publication(
                scene.store,
                forge,
                repo=_absent(scene.tmp_path),
                job=committed,
                now=_LATER,
            )

        # Unscanned, so push scans it now; the repository cannot read it.
        assert raised.value.reason == "credential_scan_unavailable"
        assert forge.reads == 0
        assert forge.pushes == []

    def _materialized_before_the_scan(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> PublicationJob:
        """A job committed by the worker as it was before the scan shipped."""
        from kew import publication_worker

        with monkeypatch.context() as unscanned:
            unscanned.setattr(
                publication_worker,
                "publication_output_guard",
                lambda store, *, run_id: lambda target: None,
            )
            job = _committed(scene)
        _mark_producer_credential_exposed(scene)
        assert job.head_sha is not None
        assert _scan_records(scene, job.head_sha) == []
        return job

    def test_a_head_materialized_before_the_scan_is_scanned_at_push(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = self._materialized_before_the_scan(scene, monkeypatch)
        forge = _Forge()

        outcome = push_publication(
            scene.store, forge, repo=scene.repo, job=job, now=_LATER
        )

        assert outcome.pushed is True
        assert forge.branches[job.branch] == job.head_sha
        (scan,) = _scan_records(scene, job.head_sha)
        assert scan.payload["verdict"] == "clean"
        assert scan.payload["base_sha"] == scene.repo.base_sha

    def test_a_head_materialized_before_the_scan_with_a_finding_is_refused(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = self._materialized_before_the_scan(scene, monkeypatch)
        _scan_finds_a_credential(monkeypatch)
        forge = _Forge()

        with pytest.raises(CredentialPublicationRefused) as raised:
            push_publication(scene.store, forge, repo=scene.repo, job=job, now=_LATER)

        assert raised.value.reason == "credential_in_output"
        assert forge.reads == 0
        assert forge.pushes == []
        (scan,) = _scan_records(scene, job.head_sha)
        assert scan.payload["verdict"] == "found"

    def test_a_found_head_is_refused_without_a_rescan_after_many_later_scans(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew import publication_credentials
        from kew.publication_scan import (
            OutputScan,
            OutputScanTarget,
            record_output_scan,
        )

        job = self._materialized_before_the_scan(scene, monkeypatch)
        assert job.head_sha is not None

        def record(head_sha: str, verdict: str) -> None:
            record_output_scan(
                scene.store,
                run_id=job.run_id,
                attempt_number=1,
                item_id="7",
                target=OutputScanTarget(
                    repository=scene.repo.path,
                    base_sha=scene.repo.base_sha,
                    head_sha=head_sha,
                ),
                result=OutputScan(
                    verdict="found" if verdict == "found" else "clean",
                    files_scanned=1,
                    matches={"value_raw": 1} if verdict == "found" else {},
                ),
            )

        record(job.head_sha, "found")
        for index in range(70):
            record(f"{index:040x}", "clean")

        def rescanned(target, values):
            raise AssertionError("a head recorded found must not be rescanned")

        monkeypatch.setattr(publication_credentials, "scan_output", rescanned)
        forge = _Forge()

        with pytest.raises(CredentialPublicationRefused) as raised:
            push_publication(scene.store, forge, repo=scene.repo, job=job, now=_LATER)

        assert raised.value.reason == "credential_in_output"
        assert forge.reads == 0
        assert forge.pushes == []
        assert [s.payload["verdict"] for s in _scan_records(scene, job.head_sha)] == [
            "found"
        ]

    def test_a_row_whose_head_moved_from_the_checked_head_pushes_nothing(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The head push_publication checks is the head it pushes.

        The caller's job names a head scanned clean; the row names another head
        with no scan. Before the fix, the gate passed on the caller's head and
        the row's unscanned head was pushed.
        """
        from kew.publication_worker import PublicationHeadStale

        checked = _committed(scene)
        assert checked.head_sha is not None
        (scan,) = _scan_records(scene, checked.head_sha)
        assert scan.payload["verdict"] == "clean"
        moved = "e" * 40
        scene.store._conn.execute(  # noqa: SLF001 — simulate a racing writer
            "UPDATE publication_job SET head_sha = ? WHERE job_id = ?",
            (moved, checked.job_id),
        )
        scene.store._conn.commit()  # noqa: SLF001
        assert scene.reread().head_sha == moved
        assert _scan_records(scene, moved) == []
        forge = _Forge()

        with pytest.raises(PublicationHeadStale) as raised:
            push_publication(
                scene.store, forge, repo=scene.repo, job=checked, now=_LATER
            )

        assert raised.value.reason == "publication_head_stale"
        assert forge.pushes == []
        assert forge.branches == {}
        # The row is left for the next tick, untouched.
        assert scene.reread().head_sha == moved
        assert scene.reread().state == checked.state

    @pytest.mark.parametrize("posture", ["env", "native", "host", "uncontained"])
    def test_a_found_head_is_refused_on_every_tick_whatever_the_posture(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch, posture: str
    ) -> None:
        """Governed review round 9 (H): a recorded finding is final for every run.

        A legacy host or uncontained job refused on one tick used to be
        admitted on the next, because only ``env:`` and native runs honoured
        the recorded ``found``.
        """
        from kew import publication_worker
        from kew.readiness import (
            Containment,
            RealizedLaunchBoundary,
            compose_realized_profile,
            compose_route_profile,
        )

        with monkeypatch.context() as unscanned:
            unscanned.setattr(
                publication_worker,
                "publication_output_guard",
                lambda store, *, run_id: lambda target: None,
            )
            job = _committed(scene)
        assert job.head_sha is not None
        if posture == "env":
            _mark_producer_credential_exposed(scene)
        else:
            scene.store.record_route_profile(
                compose_realized_profile(
                    run_id=job.run_id,
                    attempt_number=1,
                    route=compose_route_profile(
                        runner="claude-code", requested_model="claude-opus-5"
                    ),
                    boundary=RealizedLaunchBoundary(
                        containment=Containment(posture), auth_path="cli-login"
                    ),
                )
            )
        forge = _Forge()

        def tick() -> CredentialPublicationRefused:
            with pytest.raises(CredentialPublicationRefused) as raised:
                reconcile_publication(
                    scene.store,
                    scene.repo,
                    forge,
                    job=scene.reread(),
                    base_branch=_BASE_BRANCH,
                    now=_LATER,
                )
            return raised.value

        with monkeypatch.context() as finding:
            _scan_finds_a_credential(finding)
            first = tick()
        # The real scan of this head is clean; the recorded finding still wins.
        second = tick()
        third = tick()

        assert [r.reason for r in (first, second, third)] == [
            "credential_in_output"
        ] * 3
        assert forge.pushes == []
        assert forge.created == []
        assert job.branch not in forge.branches
        assert [s.payload["verdict"] for s in _scan_records(scene, job.head_sha)] == [
            "found"
        ]

    def test_reconciliation_cannot_publish_a_credential_in_the_output(
        self, scene: _Scene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mark_producer_credential_exposed(scene)
        _scan_finds_a_credential(monkeypatch)
        forge = _Forge()

        with pytest.raises(CredentialPublicationRefused):
            reconcile_publication(
                scene.store,
                scene.repo,
                forge,
                job=scene.job,
                base_branch=_BASE_BRANCH,
                now=_LATER,
            )

        assert scene.commits() == []
        assert forge.reads == 0
        assert forge.pushes == []
        assert forge.created == []

    def test_exposed_predecessor_still_needs_the_successor_head_scanned(
        self, tmp_path: Path
    ) -> None:
        store, successor = _successor_with_predecessor(tmp_path)
        forge = _Forge()
        # The row and the caller's job agree on a head with no scan record.
        store._conn.execute(  # noqa: SLF001 — a head the scan never saw
            "UPDATE publication_job SET head_sha = ? WHERE job_id = ?",
            ("f" * 40, successor.job_id),
        )
        store._conn.commit()  # noqa: SLF001
        unscanned = replace(successor, head_sha="f" * 40)

        try:
            # No head yet: nothing to release, so the lineage walk admits.
            require_normalized_job_credentials(store, successor)
            with pytest.raises(CredentialPublicationRefused) as raised:
                require_normalized_job_credentials(
                    store, successor, published_head="f" * 40
                )
            assert raised.value.reason == "credential_scan_missing"
            with pytest.raises(CredentialPublicationRefused):
                push_publication(
                    store, forge, repo=_absent(tmp_path), job=unscanned, now=_LATER
                )
            current = store.get_publication_job(successor.job_id)
            assert current is not None
            assert current.attempt_count == 0
            assert forge.reads == 0
            assert forge.pushes == []
            assert forge.created == []
        finally:
            store.close()

    def test_missing_predecessor_row_refuses_before_commit_creation(
        self, tmp_path: Path
    ) -> None:
        store, successor = _successor_with_predecessor(tmp_path)
        missing = replace(successor, predecessor_job_id="missing-job")

        try:
            with pytest.raises(CredentialPublicationRefused) as raised:
                materialize_commit(  # type: ignore[arg-type]
                    store, object(), job=missing, now=_LATER
                )
            assert raised.value.reason == "publication_evidence_unavailable"
            current = store.get_publication_job(successor.job_id)
            assert current is not None and current.attempt_count == 0
        finally:
            store.close()

    def test_inconsistent_predecessor_identity_refuses_before_remote_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, successor = _successor_with_predecessor(tmp_path)
        original_get = store.get_normalized_job

        def inconsistent(job_id: str) -> PublicationJob | None:
            job = original_get(job_id)
            if job_id == successor.predecessor_job_id and job is not None:
                return replace(job, branch="agent/issue-other")
            return job

        monkeypatch.setattr(store, "get_normalized_job", inconsistent)
        forge = _Forge()
        try:
            with pytest.raises(CredentialPublicationRefused) as raised:
                push_publication(
                    store, forge, repo=_absent(tmp_path), job=successor, now=_LATER
                )
            assert raised.value.reason == "publication_lineage_ambiguous"
            assert forge.reads == 0
            assert forge.pushes == []
        finally:
            store.close()

    def test_cyclic_predecessor_history_refuses_before_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, successor = _successor_with_predecessor(tmp_path)
        original_get = store.get_normalized_job
        original_profiles = store.list_route_profiles

        def cyclic(job_id: str) -> PublicationJob | None:
            job = original_get(job_id)
            if job_id == successor.predecessor_job_id and job is not None:
                return replace(job, predecessor_job_id=successor.job_id)
            return job

        monkeypatch.setattr(store, "get_normalized_job", cyclic)
        monkeypatch.setattr(
            store,
            "list_route_profiles",
            lambda run_id: (
                [_stored_native_profile(run_id)]
                if run_id == "run-1"
                else original_profiles(run_id)
            ),
        )
        forge = _Forge()
        try:
            with pytest.raises(CredentialPublicationRefused) as raised:
                reconcile_publication(  # type: ignore[arg-type]
                    store,
                    object(),
                    forge,
                    job=successor,
                    base_branch=_BASE_BRANCH,
                    now=_LATER,
                )
            assert raised.value.reason == "publication_lineage_cycle"
            assert forge.reads == 0
            assert forge.pushes == []
            assert forge.created == []
        finally:
            store.close()

    @pytest.mark.parametrize("guard", ["requeued-claim", "unknown-predecessor"])
    def test_incomplete_lineage_evidence_refuses_every_effect(
        self, scene: _Scene, tmp_path: Path, guard: str
    ) -> None:
        if guard == "requeued-claim":
            _requeue_unauthenticated_continuation(scene)
            store, job, repository = scene.store, scene.job, scene.repo
        else:
            unknown_dir = tmp_path / "unknown"
            unknown_dir.mkdir()
            store, job = _successor_with_predecessor(
                unknown_dir, unknown_predecessor=True
            )
            repository = object()
        forge = _Forge()
        calls = (
            lambda: materialize_commit(  # type: ignore[arg-type]
                store, repository, job=job, now=_LATER
            ),
            lambda: push_publication(  # type: ignore[arg-type]
                store, forge, repo=repository, job=job, now=_LATER
            ),
            lambda: open_publication_pull_request(
                store,
                forge,
                job=job,
                base_branch=_BASE_BRANCH,
                now=_LATER,
            ),
            lambda: finalize_publication(
                store,
                forge,
                job=job,
                base_branch=_BASE_BRANCH,
                now=_LATER,
            ),
            lambda: reconcile_publication(  # type: ignore[arg-type]
                store,
                repository,
                forge,
                job=job,
                base_branch=_BASE_BRANCH,
                now=_LATER,
            ),
        )

        try:
            for call in calls:
                with pytest.raises(CredentialPublicationRefused):
                    call()
            current = store.get_publication_job(job.job_id)
            assert current is not None and current.attempt_count == 0
            assert (
                store.artifact_receipt_attestations(
                    run_id=job.run_id, attempt_number=job.attempt_number
                )
                == []
            )
            assert forge.reads == 0
            assert forge.pushes == []
            assert forge.created == []
        finally:
            if store is not scene.store:
                store.close()


def _pushed(scene: _Scene, forge: _Forge) -> PublicationJob:
    return push_publication(
        scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
    ).job


def _opened(scene: _Scene, forge: _Forge) -> PublicationJob:
    return open_publication_pull_request(
        scene.store,
        forge,
        job=_pushed(scene, forge),
        base_branch=_BASE_BRANCH,
        now=_LATER,
    ).job


class TestThePushLandsOnceAndIsObservedBeforeEveryRepeat:
    """Task 40: crash before the push, after it, and with the answer lost."""

    def test_the_push_creates_a_branch_the_remote_did_not_have(
        self, scene: _Scene
    ) -> None:
        job = _committed(scene)
        forge = _Forge()

        outcome = push_publication(
            scene.store, forge, repo=scene.repo, job=job, now=_LATER
        )

        assert outcome.pushed is True
        assert forge.branches[job.branch] == job.head_sha
        assert forge.pushes[0].expected_head is None
        assert outcome.job.state == PUSHED
        assert outcome.job.remote_ref == job.branch

    def test_the_push_step_is_recorded_before_the_branch_moves(
        self, scene: _Scene
    ) -> None:
        """A crash between the two leaves a row saying a push was attempted and
        nothing claiming it succeeded — which is what makes the resume honest."""
        forge = _Forge()
        seen: list[tuple[str | None, str | None]] = []
        landing = forge.push_branch

        def _watch(intent: PushIntent) -> None:
            seen.append((scene.reread().last_step, forge.branches.get(intent.branch)))
            landing(intent)

        forge.push_branch = _watch  # type: ignore[method-assign]
        push_publication(
            scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
        )

        assert seen == [(STEP_PUSH, None)]

    def test_a_branch_already_at_our_head_is_bound_without_a_second_push(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        first = push_publication(
            scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
        )

        again = push_publication(
            scene.store, forge, repo=scene.repo, job=first.job, now=_NOW
        )

        assert again.pushed is False
        assert len(forge.pushes) == 1
        assert again.job.state == PUSHED
        assert len(scene.attested(PUSH_ATTESTATION)) == 1

    def test_the_push_is_attested_against_the_sealed_receipt(
        self, scene: _Scene
    ) -> None:
        job = _committed(scene)
        forge = _Forge()

        push_publication(scene.store, forge, repo=scene.repo, job=job, now=_LATER)

        pushes = scene.attested(PUSH_ATTESTATION)
        assert len(pushes) == 1
        assert pushes[0].payload["head_sha"] == job.head_sha
        assert pushes[0].payload["branch"] == job.branch
        assert pushes[0].terminal is False

    def test_a_branch_at_a_head_this_lineage_never_left_holds(
        self, scene: _Scene
    ) -> None:
        """Conflicting state holds: no force, no lease, no movement."""
        job = _committed(scene)
        forge = _Forge()
        forge.branches[job.branch] = "f" * 40

        with pytest.raises(PublicationRemoteConflictError):
            push_publication(scene.store, forge, repo=scene.repo, job=job, now=_LATER)

        assert forge.pushes == []
        assert forge.branches[job.branch] == "f" * 40
        held = scene.reread()
        assert held.state == COMMITTED
        assert held.remote_state == REMOTE_CONFLICT
        assert held.detail and job.branch in held.detail

    def test_a_lost_push_response_is_resolved_by_observing_the_branch(
        self, scene: _Scene
    ) -> None:
        """The effect landed and the answer did not come back. Looking is the
        only way to tell that apart from a push that never happened."""
        forge = _Forge()
        forge.lose_push = True

        outcome = push_publication(
            scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
        )

        assert outcome.job.state == PUSHED
        assert len(forge.pushes) == 1

    def test_a_push_that_never_landed_leaves_the_job_in_doubt(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        forge.refuse_push = RuntimeError("remote hung up")

        with pytest.raises(PublicationWorkerError, match="did not land"):
            push_publication(
                scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
            )

        doubted = scene.reread()
        assert doubted.state == AMBIGUOUS
        assert doubted.resume_state == COMMITTED

    def test_a_lease_taken_by_somebody_else_is_a_refusal_not_a_force(
        self, scene: _Scene
    ) -> None:
        """The observation said the branch was absent; by the time the push ran
        it was not. The forge rejects the lease and kew leaves it alone."""
        job = _committed(scene)
        forge = _Forge()
        landing = forge.push_branch

        def _race(intent: PushIntent) -> None:
            forge.branches[intent.branch] = "e" * 40
            landing(intent)

        forge.push_branch = _race  # type: ignore[method-assign]
        with pytest.raises(PublicationWorkerError, match="did not land"):
            push_publication(scene.store, forge, repo=scene.repo, job=job, now=_LATER)

        assert forge.branches[job.branch] == "e" * 40
        assert scene.reread().state == AMBIGUOUS

    def test_an_unreadable_remote_is_doubt_rather_than_a_push(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        forge.unreadable = RuntimeError("the forge did not answer")

        with pytest.raises(PublicationWorkerError, match="observe"):
            push_publication(
                scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
            )

        assert forge.pushes == []
        doubted = scene.reread()
        assert doubted.state == AMBIGUOUS
        assert doubted.resume_state == COMMITTED

    def test_a_credential_in_a_push_failure_never_reaches_the_row(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        forge.refuse_push = RuntimeError(
            f"fatal: authentication failed for token {_LEAKY_TOKEN}"
        )

        with pytest.raises(PublicationWorkerError) as err:
            push_publication(
                scene.store, forge, repo=scene.repo, job=_committed(scene), now=_LATER
            )

        detail = scene.reread().detail or ""
        assert _LEAKY_TOKEN not in detail and "<redacted>" in detail
        assert _LEAKY_TOKEN not in str(err.value)


class TestThePullRequestIsOpenedOnceAndBoundToTheLineage:
    """Task 40: duplicate finalizers, lost creates, and a PR the agent opened."""

    def test_reconciling_opens_one_pull_request_bound_to_the_branch(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()

        outcome = open_publication_pull_request(
            scene.store,
            forge,
            job=_pushed(scene, forge),
            base_branch=_BASE_BRANCH,
            now=_LATER,
        )

        assert outcome.created is True
        assert len(forge.created) == 1
        assert outcome.job.state == OPENED
        assert outcome.job.remote_ref == outcome.pull_request.url
        assert forge.created[0].base_branch == _BASE_BRANCH

    def test_the_pr_body_carries_the_marker_that_identifies_the_job(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()

        job = _opened(scene, forge)

        assert job.job_id in forge.created[0].body

    def test_a_second_reconcile_opens_no_second_pull_request(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        first = _opened(scene, forge)

        again = open_publication_pull_request(
            scene.store, forge, job=first, base_branch=_BASE_BRANCH, now=_NOW
        )

        assert again.created is False
        assert len(forge.created) == 1
        assert len(scene.attested(PULL_REQUEST_ATTESTATION)) == 1

    def test_an_agent_opened_pr_is_bound_rather_than_duplicated_on_reconcile(
        self, scene: _Scene
    ) -> None:
        """The compatibility finding is recorded: same branch, same base, no
        marker — so the evidence says the host bound one it did not open."""
        forge = _Forge()
        job = _pushed(scene, forge)
        forge.pulls.append(
            ObservedPullRequest(
                number=41,
                url="https://forge.invalid/pull/41",
                head_branch=job.branch,
                base_branch=_BASE_BRANCH,
                head_sha=job.head_sha,
                body="opened by the agent",
            )
        )

        outcome = open_publication_pull_request(
            scene.store, forge, job=job, base_branch=_BASE_BRANCH, now=_LATER
        )

        assert outcome.created is False
        assert forge.created == []
        assert outcome.pull_request.number == 41
        attested = scene.attested(PULL_REQUEST_ATTESTATION)[0].payload
        assert attested["created"] is False
        assert attested["marker_present"] is False
        assert attested["pull_request_url"] == "https://forge.invalid/pull/41"

    def test_a_pr_onto_another_base_holds_when_reconciling(self, scene: _Scene) -> None:
        forge = _Forge()
        job = _pushed(scene, forge)
        forge.pulls.append(
            ObservedPullRequest(
                number=41,
                url="https://forge.invalid/pull/41",
                head_branch=job.branch,
                base_branch="release/1.x",
                head_sha=job.head_sha,
            )
        )

        with pytest.raises(PublicationRemoteConflictError):
            open_publication_pull_request(
                scene.store, forge, job=job, base_branch=_BASE_BRANCH, now=_LATER
            )

        assert forge.created == []
        held = scene.reread()
        assert held.state == PUSHED
        assert held.remote_state == REMOTE_CONFLICT

    def test_a_closed_pr_holds_rather_than_reopening_on_reconcile(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        job = _pushed(scene, forge)
        forge.pulls.append(
            ObservedPullRequest(
                number=41,
                url="https://forge.invalid/pull/41",
                head_branch=job.branch,
                base_branch=_BASE_BRANCH,
                head_sha=job.head_sha,
                state=PR_CLOSED,
            )
        )

        with pytest.raises(PublicationRemoteConflictError, match="closed"):
            open_publication_pull_request(
                scene.store, forge, job=job, base_branch=_BASE_BRANCH, now=_LATER
            )

        assert forge.created == []

    def test_a_lost_pr_create_response_is_resolved_by_looking_it_up(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        forge.lose_create = True

        outcome = open_publication_pull_request(
            scene.store,
            forge,
            job=_pushed(scene, forge),
            base_branch=_BASE_BRANCH,
            now=_LATER,
        )

        assert len(forge.created) == 1
        assert outcome.job.state == OPENED
        assert outcome.pull_request.url == forge.pulls[0].url

    def test_the_pr_create_step_is_recorded_before_the_pull_request_exists(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        seen: list[tuple[str | None, int]] = []
        opening = forge.create_pull_request

        def _watch(intent: PullRequestIntent) -> ObservedPullRequest:
            seen.append((scene.reread().last_step, len(forge.pulls)))
            return opening(intent)

        forge.create_pull_request = _watch  # type: ignore[method-assign]
        open_publication_pull_request(
            scene.store,
            forge,
            job=_pushed(scene, forge),
            base_branch=_BASE_BRANCH,
            now=_LATER,
        )

        assert seen == [(STEP_PR_CREATE, 0)]

    def test_an_unreadable_forge_opens_no_pr_and_leaves_doubt(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        job = _pushed(scene, forge)
        forge.unreadable = RuntimeError("the forge did not answer")

        with pytest.raises(PublicationWorkerError, match="observe"):
            open_publication_pull_request(
                scene.store, forge, job=job, base_branch=_BASE_BRANCH, now=_LATER
            )

        assert forge.created == []
        doubted = scene.reread()
        assert doubted.state == AMBIGUOUS
        assert doubted.resume_state == PUSHED


class TestOneReconciliationLeavesOneRemoteResult:
    """Task 42: repeat it, crash it, race it — one branch and one pull request.

    The commit step could prove this by recomputation. Nothing out here can, so
    the proof is behavioural: every one of these tests runs the reconciliation
    more than once, or interrupts it and runs it again, and then counts what the
    forge actually holds.
    """

    def _reconcile(self, scene: _Scene, forge: _Forge, *, now: datetime = _LATER):
        return reconcile_publication(
            scene.store,
            scene.repo,
            forge,
            job=scene.reread(),
            base_branch=_BASE_BRANCH,
            now=now,
        )

    def test_one_reconcile_commits_pushes_opens_and_finalizes(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()

        outcome = self._reconcile(scene, forge)

        assert outcome.state == RECONCILED
        assert forge.branches[outcome.branch] == outcome.head_sha
        assert outcome.pull_request_url == forge.pulls[0].url
        assert (outcome.branch_pushed, outcome.pull_request_created) == (True, True)
        settled = scene.reread()
        assert settled.active is False
        assert settled.remote_ref == outcome.pull_request_url

    def test_repeating_the_reconcile_leaves_one_branch_and_one_pull_request(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        first = self._reconcile(scene, forge)

        again = self._reconcile(scene, forge, now=_NOW)
        third = self._reconcile(scene, forge, now=_NOW)

        assert len(forge.pushes) == 1
        assert len(forge.created) == 1
        assert (again.head_sha, third.head_sha) == (first.head_sha, first.head_sha)
        assert again.pull_request_url == first.pull_request_url
        assert (again.branch_pushed, again.pull_request_created) == (False, False)

    def test_a_reconciled_job_is_never_redispatched(self, scene: _Scene) -> None:
        """Not one read of the forge: completed work has nothing to observe."""
        forge = _Forge()
        first = self._reconcile(scene, forge)
        forge.reads = 0

        again = self._reconcile(scene, forge, now=_NOW)

        assert forge.reads == 0
        assert (again.pull_request_url, again.pull_request_number) == (
            first.pull_request_url,
            first.pull_request_number,
        )

    def test_each_publication_fact_is_attested_exactly_once(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        self._reconcile(scene, forge)
        self._reconcile(scene, forge, now=_NOW)

        kinds = [
            recorded.kind
            for recorded in scene.store.artifact_receipt_attestations(
                run_id="run-1", attempt_number=1
            )
        ]
        assert kinds == [
            COMMIT_ATTESTATION,
            PUSH_ATTESTATION,
            PULL_REQUEST_ATTESTATION,
            RECONCILE_ATTESTATION,
        ]

    def test_the_sealed_receipt_still_stands_after_a_reconcile(
        self, scene: _Scene
    ) -> None:
        """Publication appends to the chain; it never rewrites the receipt."""
        before = scene.store.get_artifact_receipt(run_id="run-1", attempt_number=1)
        forge = _Forge()

        self._reconcile(scene, forge)

        after = scene.store.get_artifact_receipt(run_id="run-1", attempt_number=1)
        assert after == before
        assert scene.store.verify_artifact_receipt(run_id="run-1", attempt_number=1).ok
        assert all(
            recorded.terminal is False
            for recorded in scene.store.artifact_receipt_attestations(
                run_id="run-1", attempt_number=1
            )
        )

    def test_a_crash_between_the_push_and_the_row_reconciles_to_one_push(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()

        def _explode(**kwargs: object) -> PublicationJob:
            raise OSError("the host died before the row moved")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(scene.store, "observe_publication_remote", _explode)
            with pytest.raises(OSError, match="died"):
                self._reconcile(scene, forge)

        assert scene.reread().state == COMMITTED
        assert len(scene.attested(PUSH_ATTESTATION)) == 1

        outcome = self._reconcile(scene, forge, now=_NOW)

        assert len(forge.pushes) == 1
        assert outcome.state == RECONCILED
        assert len(scene.attested(PUSH_ATTESTATION)) == 1

    def test_a_crash_between_the_pr_create_and_the_row_leaves_one_pull_request(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        pushed = _pushed(scene, forge)

        def _explode(**kwargs: object) -> PublicationJob:
            raise OSError("the host died before the row moved")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(scene.store, "observe_publication_remote", _explode)
            with pytest.raises(OSError, match="died"):
                open_publication_pull_request(
                    scene.store,
                    forge,
                    job=pushed,
                    base_branch=_BASE_BRANCH,
                    now=_LATER,
                )

        assert scene.reread().state == PUSHED

        outcome = self._reconcile(scene, forge, now=_NOW)

        assert len(forge.created) == 1
        assert outcome.pull_request_created is False
        assert outcome.state == RECONCILED
        assert len(scene.attested(PULL_REQUEST_ATTESTATION)) == 1

    def test_a_reconcile_resumes_a_job_left_ambiguous_by_a_lost_push(
        self, scene: _Scene
    ) -> None:
        """The branch is at our head and the row says nothing but doubt. Looking
        is what ends that, and it ends it at the state the evidence supports."""
        forge = _Forge()
        job = _committed(scene)
        forge.branches[job.branch] = job.head_sha or ""
        scene.store.observe_publication_remote(
            job_id=job.job_id,
            observation=RemoteObservation(
                kind=REMOTE_UNKNOWN, detail="the forge did not answer"
            ),
            now=_LATER,
        )
        assert scene.reread().state == AMBIGUOUS

        outcome = self._reconcile(scene, forge, now=_NOW)

        assert forge.pushes == []
        assert outcome.state == RECONCILED

    def test_a_duplicate_finalizer_settles_the_job_once(self, scene: _Scene) -> None:
        forge = _Forge()
        opened = _opened(scene, forge)

        first = finalize_publication(
            scene.store, forge, job=opened, base_branch=_BASE_BRANCH, now=_LATER
        )
        second = finalize_publication(
            scene.store, forge, job=opened, base_branch=_BASE_BRANCH, now=_NOW
        )

        assert (first.state, second.state) == (RECONCILED, RECONCILED)
        assert second.updated_at == first.updated_at
        assert len(scene.attested(RECONCILE_ATTESTATION)) == 1

    def test_finalizing_holds_when_the_branch_was_reset_off_our_head(
        self, scene: _Scene
    ) -> None:
        """Settling claims the remote holds this publication. Somebody rewound
        the branch to the base, so it does not — and saying so is the point."""
        forge = _Forge()
        opened = _opened(scene, forge)
        forge.branches[opened.branch] = scene.repo.base_sha

        with pytest.raises(PublicationRemoteConflictError, match="no longer holds"):
            finalize_publication(
                scene.store, forge, job=opened, base_branch=_BASE_BRANCH, now=_LATER
            )

        held = scene.reread()
        assert held.state == OPENED
        assert held.remote_state == REMOTE_CONFLICT
        assert scene.attested(RECONCILE_ATTESTATION) == []

    def test_finalizing_holds_when_the_branch_carries_somebody_elses_work(
        self, scene: _Scene
    ) -> None:
        forge = _Forge()
        opened = _opened(scene, forge)
        forge.branches[opened.branch] = "f" * 40

        with pytest.raises(PublicationRemoteConflictError, match="never left"):
            finalize_publication(
                scene.store, forge, job=opened, base_branch=_BASE_BRANCH, now=_LATER
            )

        assert scene.reread().state == OPENED
        assert scene.attested(RECONCILE_ATTESTATION) == []

    def _successor(self, scene: _Scene) -> PublicationJob:
        tree = _promote(scene.repo, scene.tmp_path, b"the successor package\n")
        digest = _seal(
            scene.store,
            base_sha=scene.repo.base_sha,
            tree_sha=tree,
            attempt_number=2,
        )
        scene.store.record_route_profile(_native_profile(scene.job.run_id))
        return scene.store.open_successor_publication_job(
            intent=_intent(
                digest, base_sha=scene.repo.base_sha, tree_sha=tree, attempt_number=2
            ),
            now=_LATER,
        )

    def test_a_successor_reconcile_fast_forwards_and_keeps_one_pull_request(
        self, scene: _Scene
    ) -> None:
        """A second SHA for the same lineage takes the branch on from where its
        predecessor left it, and adopts the pull request already open on it."""
        forge = _Forge()
        predecessor = _opened(scene, forge)
        successor = self._successor(scene)

        outcome = reconcile_publication(
            scene.store,
            scene.repo,
            forge,
            job=successor,
            base_branch=_BASE_BRANCH,
            now=_LATER,
        )

        assert len(forge.created) == 1
        assert outcome.pull_request_url == forge.pulls[0].url
        assert forge.pushes[-1].expected_head == predecessor.head_sha
        assert forge.branches[outcome.branch] == outcome.head_sha
        assert outcome.head_sha != predecessor.head_sha
        assert outcome.state == RECONCILED
        assert len(scene.attested(PUSH_ATTESTATION, attempt_number=2)) == 1

    def test_a_successor_to_a_settled_publication_holds_rather_than_forcing(
        self, scene: _Scene
    ) -> None:
        """Nothing supersedes a job that already reconciled, so a later attempt
        starts from the base — and its commit is a sibling of what is published,
        not a descendant. Landing it would take a force, so it holds instead."""
        forge = _Forge()
        settled = self._reconcile(scene, forge)
        successor = self._successor(scene)
        assert successor.predecessor_job_id is None

        with pytest.raises(PublicationRemoteConflictError, match="never left"):
            reconcile_publication(
                scene.store,
                scene.repo,
                forge,
                job=successor,
                base_branch=_BASE_BRANCH,
                now=_LATER,
            )

        assert len(forge.pushes) == 1
        assert forge.branches[successor.branch] == settled.head_sha


class TestNothingCanReconcileYetEither:
    """Task 42: the reconciler is as unreachable as the commit step it extends."""

    def test_the_selector_is_still_shut_for_a_reconcile(self, scene: _Scene) -> None:
        with pytest.raises(PublicationJobsInactiveError):
            reconcile_next(
                scene.store,
                scene.repo,
                _Forge(),
                base_branch=_BASE_BRANCH,
                now=_LATER,
            )

    def test_every_entry_point_is_handed_a_repository_and_never_a_path(self) -> None:
        for entry in (materialize_commit, materialize_next, reconcile_next):
            second = list(inspect.signature(entry).parameters.values())[1]
            assert second.annotation == "CanonicalRepository"

    def test_the_worker_ships_no_forge_of_its_own(self) -> None:
        """Source inspection: the remote is a structural type with four verbs
        and no implementation, so there is nothing here that could authenticate
        to a forge before E7 says a publication may start."""
        source = (_WORKER_SOURCE / "publication_worker.py").read_text(encoding="utf-8")

        assert "class PublicationRemote(Protocol)" in source
        for reaching_out in ("http", "requests", "urllib", "TOOL_PATHS"):
            assert reaching_out not in source

    @pytest.mark.policy
    def test_no_dispatch_or_product_verb_reconciles(self) -> None:
        # E5 publication-job recovery stays dormant (the 2026-08-25 ADR).
        # Credential revocation admission is separate: C3/C4 require it before
        # synchronous publication. Match worker entry points as whole names.
        worker_entry = re.compile(r"\b(?:reconcile_publication|reconcile_next)\b")
        naming = {
            path
            for path in _WORKER_SOURCE.rglob("*.py")
            if path.name != "publication_worker.py"
            and worker_entry.search(path.read_text(encoding="utf-8"))
        }

        assert naming == set()
