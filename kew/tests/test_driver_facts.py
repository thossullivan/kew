import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from support.checkout_generations import fixture_checkout_generation
from support.pr_batch import (
    batched_numbers,
    is_pr_batch,
    pr_batch_payload,
    pr_batch_result,
)

from kew.graph.driver.facts import (
    GitHubFactsReader,
    NodeFacts,
    RepositoryPermission,
    ReviewPressure,
    _implementation_pr_runs,
    read_no_change_facts,
    read_review_pressure,
)
from kew.graph.driver.overlay import DriverStatus, overlay_status
from kew.human_review import (
    REVIEW_PENDING,
    PathChange,
    ReviewPolicy,
    ReviewPolicyError,
    decide_publication,
    measure_risk,
)
from kew.no_change import (
    ALREADY_SATISFIED_CONTRACT_VERSION,
    AUTHORIZED,
    CLOSE_IF_HOST_VERIFIED,
    EFFECT_PENDING,
    REJECTED,
    SYNC_PENDING,
    AlreadySatisfiedContract,
    NamedCheck,
    NoChangeEvidence,
    NoChangeIntent,
)
from kew.publication_job import (
    REVIEW_REMOTE_AVAILABLE,
    PublicationIntent,
    PublicationJobValueError,
    RemoteReviewObservation,
    ReviewObligation,
    unavailable_review_observation,
)
from kew.publication_repository import VERIFICATION_EXACT_TREE
from kew.run_store import ArtifactLineage, RunRecord, SqliteRunStore
from kew.run_store_publication_jobs import PublicationReview


def _result(stdout: str, returncode: int = 0, stderr: str = ""):
    class R:
        pass

    r = R()
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = stderr
    return r


def test_issue_facts_requests_state_reason() -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _result(
            '{"number":5,"state":"CLOSED","stateReason":"NOT_PLANNED","labels":[]}'
        )

    reader = GitHubFactsReader("o/r", run=fake_run)
    facts = reader.issue_facts(5)
    assert "stateReason" in calls[0][calls[0].index("--json") + 1]
    assert facts.state_reason == "NOT_PLANNED"


def test_issue_facts_no_linked_pr() -> None:
    def fake_run(cmd, **kw):
        return _result(
            json.dumps(
                {
                    "number": 5,
                    "state": "OPEN",
                    "stateReason": None,
                    "labels": [{"name": "blocked"}],
                    "closedByPullRequestsReferences": [],
                }
            )
        )

    reader = GitHubFactsReader("o/r", run=fake_run)
    facts = reader.issue_facts(5)
    assert facts == NodeFacts(
        issue_num=5,
        state="OPEN",
        state_reason=None,
        labels=frozenset({"blocked"}),
        pr_number=None,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
    )


def test_issue_facts_enriches_from_linked_pr() -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(
                json.dumps(
                    {
                        "number": 7,
                        "state": "OPEN",
                        "stateReason": None,
                        "labels": [],
                        "closedByPullRequestsReferences": [
                            {"number": 42, "url": "https://github.com/o/r/pull/42"}
                        ],
                    }
                )
            )
        if cmd[:3] == ["gh", "pr", "view"]:
            return _result(
                json.dumps(
                    {
                        "number": 42,
                        "state": "OPEN",
                        "reviewDecision": "APPROVED",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "commits": [
                            {"committedDate": "2026-06-30T00:00:00Z"},
                            {"committedDate": "2026-07-01T00:00:00Z"},
                        ],
                        "latestReviews": [],
                        "author": {"login": "someone"},
                    }
                )
            )
        if cmd[:3] == ["gh", "pr", "list"]:
            return _result("[]")
        raise AssertionError(f"unexpected call: {cmd}")

    reader = GitHubFactsReader("o/r", run=fake_run)
    facts = reader.issue_facts(7)
    assert facts.pr_number == 42
    assert facts.pr_url == "https://github.com/o/r/pull/42"
    assert facts.review_decision == "APPROVED"
    assert facts.merge_state_status == "CLEAN"
    assert facts.head_oid == "abc123"
    assert facts.pr_pushed_at == "2026-07-01T00:00:00Z"
    # An unprepared reader has no failed or saturated batch to justify fallback.
    assert len(calls) == 2


def test_prepare_discovers_external_pr_for_awaiting_publication_branch(
    tmp_path,
) -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if is_pr_batch(cmd):
            return pr_batch_result(
                cmd,
                [
                    {
                        "number": 77,
                        "url": "https://github.com/o/r/pull/77",
                        "state": "OPEN",
                        "headRefName": "agent/issue-42",
                        "reviewDecision": None,
                        "mergeStateStatus": "BLOCKED",
                        "headRefOid": "abc123",
                        "commits": [],
                        "latestReviews": [],
                        "author": {"login": "agent"},
                    }
                ],
            )
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(
                json.dumps(
                    {
                        "number": 42,
                        "state": "OPEN",
                        "stateReason": None,
                        "labels": [],
                        "closedByPullRequestsReferences": [],
                    }
                )
            )
        raise AssertionError(f"unexpected call: {cmd}")

    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="awaiting_publication",
                exit_code=0,
                started_at=now,
                finished_at=now,
                error_category="publication_unavailable",
            )
        )
        store.record_continuation(
            "epic:x",
            "42",
            "run-1",
            "agent/issue-42",
            tmp_path / "preserved",
            checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
        )
        reader = GitHubFactsReader("o/r", store=store, run=fake_run)

        reader.prepare(["42"])
        facts = reader.issue_facts(42)
        reconciled = store.get("run-1")
        continuation = store.list_continuations("epic:x")[0]

    assert facts.pr_number == 77
    assert facts.pr_url == "https://github.com/o/r/pull/77"
    assert reconciled is not None
    assert reconciled.status == "success"
    assert reconciled.pr_url == facts.pr_url
    assert continuation.state == "terminal"
    assert [call[:3] for call in calls] == [
        ["gh", "api", "graphql"],
        ["gh", "issue", "view"],
    ]


def test_provider_completed_reconciles_awaiting_publication_without_pr(
    tmp_path,
) -> None:
    now = datetime.now(UTC)

    def fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(
                json.dumps(
                    {
                        "number": 42,
                        "state": "CLOSED",
                        "stateReason": "COMPLETED",
                        "labels": [],
                        "closedByPullRequestsReferences": [],
                    }
                )
            )
        if cmd[:3] == ["gh", "pr", "list"]:
            return _result("[]")
        raise AssertionError(f"unexpected call: {cmd}")

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="awaiting_publication",
                exit_code=0,
                started_at=now,
                finished_at=now,
            )
        )
        reader = GitHubFactsReader("o/r", store=store, run=fake_run)

        facts = reader.issue_facts(42)
        reconciled = store.get("run-1")

    assert facts.state == "CLOSED"
    assert facts.state_reason == "COMPLETED"
    assert reconciled is not None
    assert reconciled.status == "success"


def test_remote_fact_cannot_finalize_credential_protected_output(tmp_path) -> None:
    from kew.graph.driver.facts import reconcile_awaiting_publication

    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="protected-run",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="awaiting_publication",
                exit_code=0,
                started_at=now,
                finished_at=now,
                container_name="kew-42",
            )
        )

        changed = reconcile_awaiting_publication(
            store,
            item_id="42",
            repo="o/r",
            pr_url="https://example.invalid/pr/42",
            observation="open PR",
        )
        refused = store.get("protected-run")
        events = store.list_audit_events(event="credential_publication_refused")

    assert changed is False
    assert refused is not None
    assert refused.status == "refused"
    assert refused.error_category == "credential_publication_refused"
    assert len(events) == 1


def test_ci_fix_run_never_supplies_an_implementation_pr() -> None:
    runs = [
        RunRecord(
            run_id="ci-fix",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            run_kind="ci_fix",
            pr_url="https://github.com/o/r/pull/42",
        )
    ]

    assert _implementation_pr_runs(None, "42", "agent/issue-42", runs=runs) == []


def test_ci_fix_cannot_replace_deferred_implementation_reconciliation(tmp_path) -> None:
    from kew.graph.driver.facts import reconcile_awaiting_publication

    started = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        for run_id, run_kind, offset in (
            ("implementation", "dispatch", 0),
            ("ci-fix", "ci_fix", 1),
        ):
            store.save(
                RunRecord(
                    run_id=run_id,
                    issue_num=42,
                    item_id="42",
                    branch="agent/issue-42",
                    status="awaiting_publication",
                    exit_code=1,
                    started_at=started + timedelta(seconds=offset),
                    run_kind=run_kind,
                )
            )

        assert reconcile_awaiting_publication(
            store,
            item_id="42",
            repo="o/r",
            pr_url="https://example.invalid/pr/42",
            observation="open PR",
        )
        implementation = store.get("implementation")
        ci_fix = store.get("ci-fix")

    assert implementation is not None and implementation.status == "success"
    assert ci_fix is not None and ci_fix.status == "awaiting_publication"


def test_issue_facts_handles_gh_failure_gracefully() -> None:
    def fake_run(cmd, **kw):
        return _result("", returncode=1, stderr="not found")

    reader = GitHubFactsReader("o/r", run=fake_run)
    facts = reader.issue_facts(999)
    assert facts.issue_num == 999
    assert facts.state == ""
    assert facts.state_reason is None
    assert facts.pr_number is None


def test_pr_facts_returns_parsed_json() -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _result(
            json.dumps(
                {
                    "number": 42,
                    "state": "OPEN",
                    "reviewDecision": "APPROVED",
                    "mergeStateStatus": "CLEAN",
                    "headRefOid": "abc123",
                    "commits": [],
                    "latestReviews": [],
                    "author": {"login": "someone"},
                }
            )
        )

    reader = GitHubFactsReader("o/r", run=fake_run)
    facts = reader.pr_facts(42)
    assert facts["number"] == 42
    assert facts["mergeStateStatus"] == "CLEAN"
    cmd = calls[0]
    assert cmd[:3] == ["gh", "pr", "view"]
    json_fields = cmd[cmd.index("--json") + 1]
    for field in (
        "number",
        "state",
        "reviewDecision",
        "mergeStateStatus",
        "headRefOid",
        "commits",
        "latestReviews",
        "author",
    ):
        assert field in json_fields


def test_pr_facts_returns_empty_dict_on_failure() -> None:
    def fake_run(cmd, **kw):
        return _result("", returncode=1)

    reader = GitHubFactsReader("o/r", run=fake_run)
    assert reader.pr_facts(999) == {}


def test_fresh_pr_facts_bypasses_this_ticks_prepared_batch() -> None:
    """#2148: the merge queue's post-verification head check must see a push that
    landed mid-tick. Through ``pr_facts`` it re-read the batch's own head and so
    could never disagree with the classification it exists to re-check."""
    batched = {
        "number": 42,
        "state": "OPEN",
        "headRefName": "agent/issue-7",
        "headRefOid": "aaaaaaa",
    }
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if is_pr_batch(cmd):
            return pr_batch_result(cmd, [batched])
        return _result(json.dumps({**batched, "headRefOid": "bbbbbbb"}))

    reader = GitHubFactsReader("o/r", run=fake_run)
    reader.prepare(["7"])
    calls.clear()

    # The cached read is the batch's head, and costs no call.
    assert reader.pr_facts(42)["headRefOid"] == "aaaaaaa"
    assert calls == []

    # The fresh read goes to GitHub and sees the moved head.
    assert reader.fresh_pr_facts(42)["headRefOid"] == "bbbbbbb"
    assert [c[:3] for c in calls] == [["gh", "pr", "view"]]


def test_repository_permission_reads_current_collaborator_permission() -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _result(
            json.dumps(
                {
                    "permission": "write",
                    "user": {"login": "alice", "type": "User"},
                }
            )
        )

    reader = GitHubFactsReader("o/r", run=fake_run)

    assert reader.repository_permission("alice") == RepositoryPermission(
        login="alice", actor_type="User", permission="write"
    )
    assert calls == [["gh", "api", "repos/o/r/collaborators/alice/permission"]]


def test_repository_permission_fails_closed_for_bad_evidence() -> None:
    payloads = [
        ("", 1),
        ("not-json", 0),
        (
            json.dumps(
                {"permission": "write", "user": {"login": "mallory", "type": "User"}}
            ),
            0,
        ),
        (
            json.dumps(
                {"permission": "write", "user": {"login": "alice", "type": "Bot"}}
            ),
            0,
        ),
        (json.dumps({"user": {"login": "alice", "type": "User"}}), 0),
    ]

    for stdout, returncode in payloads:
        reader = GitHubFactsReader(
            "o/r",
            run=lambda cmd, stdout=stdout, returncode=returncode, **kw: _result(
                stdout, returncode=returncode
            ),
        )
        assert reader.repository_permission("alice") is None


def test_head_checks_reads_every_run_and_holds_skipped_tests() -> None:
    """#2033: the driver decides a head's checks the way the ci_green gate does."""
    seen: list[list[str]] = []
    runs = (
        '{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"}\n'
        '{"id": 2, "name": "test", "status": "completed", "conclusion": "skipped"}\n'
    )

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return _result(runs)

    reader = GitHubFactsReader("o/r", run=fake_run)
    state, evidence = reader.head_checks("a" * 40, ["test", "agent-image-scan"])
    assert state == "awaiting_approval"
    assert "awaiting ci-approved: tests did not run (test skipped)" in evidence
    assert f"repos/o/r/commits/{'a' * 40}/check-runs?filter=all" in seen[0]


def test_head_checks_green_only_after_a_real_success() -> None:
    runs = (
        '{"id": 1, "name": "test", "status": "completed", "conclusion": "skipped"}\n'
        '{"id": 5, "name": "test", "status": "completed", "conclusion": "success"}\n'
    )
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result(runs))
    assert reader.head_checks("a" * 40, ["test", "agent-image-scan"])[0] == "success"


def _statuses_only_reader(state: str, total: int) -> GitHubFactsReader:
    """A head with no check runs whose CI posts legacy commit statuses."""

    def fake_run(cmd, **kw):
        if any("/check-runs" in part for part in cmd):
            return _result("")
        assert any(part.endswith("/status") for part in cmd), cmd
        return _result(json.dumps({"state": state, "total_count": total}))

    return GitHubFactsReader("o/r", run=fake_run)


@pytest.mark.parametrize(
    ("state", "total", "expected"),
    [
        # A commit status never makes a head green on its own (#2033).
        ("success", 2, "no_evidence"),
        ("pending", 1, "pending"),
        ("failure", 1, "failure"),
        ("error", 1, "failure"),
        ("pending", 0, "no_evidence"),
    ],
)
def test_head_checks_reads_legacy_statuses_when_no_check_runs(
    state: str, total: int, expected: str
) -> None:
    decided, evidence = _statuses_only_reader(state, total).head_checks(
        "a" * 40, ["test", "agent-image-scan"]
    )
    assert decided == expected
    if state == "success":
        assert evidence == "commit statuses green but no check runs: merge by hand"
    elif expected == "no_evidence":
        assert evidence == "no CI evidence on head: merge by hand"


def test_an_early_success_status_waits_until_check_runs_decide() -> None:
    """A bot's success status can land before the head's Actions runs
    register. It is not green; the check runs, once there, decide."""
    runs: list[str] = []

    def fake_run(cmd, **kw):
        if any("/check-runs" in part for part in cmd):
            return _result("".join(runs))
        return _result(json.dumps({"state": "success", "total_count": 1}))

    reader = GitHubFactsReader("o/r", run=fake_run)
    assert reader.head_checks("a" * 40, ["test", "agent-image-scan"]) == (
        "no_evidence",
        "commit statuses green but no check runs: merge by hand",
    )
    runs.append(
        '{"id": 1, "name": "test", "status": "completed", "conclusion": "success"}\n'
    )
    assert reader.head_checks("a" * 40, ["test", "agent-image-scan"])[0] == "success"


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        # A push-only job skipped beside the real test run: merge.
        (
            [("test", "success"), ("publish-docs", "skipped")],
            "success",
        ),
        # The approval-gated test job skipped: wait for ci-approved.
        ([("lint", "success"), ("test", "skipped")], "awaiting_approval"),
        # A gated matrix leg skipped: wait too.
        ([("lint", "success"), ("test (3.12)", "skipped")], "awaiting_approval"),
        # Only a non-gated skipped job: no evidence the head was tested.
        ([("publish-docs", "skipped")], "no_checks"),
    ],
)
def test_head_checks_waits_only_on_approval_gated_checks(
    runs: list[tuple[str, str]], expected: str
) -> None:
    stdout = "".join(
        json.dumps({"id": i, "name": n, "status": "completed", "conclusion": c}) + "\n"
        for i, (n, c) in enumerate(runs, 1)
    )
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result(stdout))
    assert reader.head_checks("a" * 40, ["test", "agent-image-scan"])[0] == expected


def test_head_checks_unreadable_is_pending_not_green() -> None:
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result("", returncode=1))
    state, evidence = reader.head_checks("a" * 40, ["test", "agent-image-scan"])
    assert state == "pending"
    assert "unreadable" in evidence


def test_required_checks_green_clean() -> None:
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result("[]"))
    assert reader.required_checks_green("main", {"mergeStateStatus": "CLEAN"}) is True


def test_required_checks_green_has_hooks_is_clean_equivalent() -> None:
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result("[]"))
    assert (
        reader.required_checks_green("main", {"mergeStateStatus": "HAS_HOOKS"}) is True
    )


def test_required_checks_green_false_for_blocked_states() -> None:
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result("[]"))
    for state in ("BLOCKED", "BEHIND", "DIRTY", "UNSTABLE"):
        assert (
            reader.required_checks_green("main", {"mergeStateStatus": state}) is False
        )


def test_required_checks_green_unknown_is_not_green() -> None:
    reader = GitHubFactsReader("o/r", run=lambda cmd, **kw: _result("[]"))
    assert (
        reader.required_checks_green("main", {"mergeStateStatus": "UNKNOWN"}) is False
    )


# ---------------------------------------------------------------------------
# I15: human-review backpressure facts. The driver READS the E5/I14 capacity
# truth — it never recounts it, and it never turns "we could not ask" into
# "nothing is waiting".
# ---------------------------------------------------------------------------


class _Job:
    """The one field `read_review_pressure` needs off a publication job."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


class _FakeCapacityStore:
    """A store that answers only the four authoritative capacity reads."""

    def __init__(
        self,
        *,
        observation: RemoteReviewObservation | None = None,
        effective: ReviewPolicy | None = None,
        occupancy: int = 0,
        reviews: dict[str, PublicationReview] | None = None,
        raises: Exception | None = None,
        occupancy_raises: Exception | None = None,
    ) -> None:
        self._observation = observation
        self._effective = effective
        self._occupancy = occupancy
        self._reviews = reviews or {}
        self._raises = raises
        self._occupancy_raises = occupancy_raises
        self.occupancy_calls: list[str] = []

    def review_observation(self, delivery_repo: str) -> RemoteReviewObservation | None:
        if self._raises is not None:
            raise self._raises
        return self._observation

    def effective_human_review_policy(
        self, *, system: ReviewPolicy, now: datetime
    ) -> ReviewPolicy:
        return self._effective or system

    def review_capacity_occupancy(
        self, *, delivery_repo: str, observation: RemoteReviewObservation
    ) -> int:
        self.occupancy_calls.append(delivery_repo)
        if self._occupancy_raises is not None:
            raise self._occupancy_raises
        return self._occupancy

    def list_publication_jobs(self) -> list[_Job]:
        return [_Job(job_id) for job_id in self._reviews]

    def publication_review(self, job_id: str) -> PublicationReview | None:
        return self._reviews.get(job_id)


_PRESSURE_REPO = "o/r"
_PRESSURE_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _system_policy(**overrides: object) -> ReviewPolicy:
    fields: dict[str, object] = {
        "delivery_repo": _PRESSURE_REPO,
        "max_pending_reviews": 3,
    }
    fields.update(overrides)
    return ReviewPolicy(**fields)  # type: ignore[arg-type]


def _seen(**overrides: object) -> RemoteReviewObservation:
    fields: dict[str, object] = {
        "delivery_repo": _PRESSURE_REPO,
        "availability": REVIEW_REMOTE_AVAILABLE,
        "observed_at": _PRESSURE_NOW.isoformat(),
        "generation": 4,
        "obligations": (),
    }
    fields.update(overrides)
    return RemoteReviewObservation(**fields)  # type: ignore[arg-type]


def _held_review(
    job_id: str,
    *,
    delivery_repo: str = _PRESSURE_REPO,
    state: str = REVIEW_PENDING,
    reserved_at: str | None = None,
    added: int = 0,
    deleted: int = 0,
) -> PublicationReview:
    return PublicationReview(
        job_id=job_id,
        delivery_repo=delivery_repo,
        review_state=state,
        risk_added_lines=added,
        risk_deleted_lines=deleted,
        risk_paths=1,
        risk_digest="risk-1",
        risk_reasons=(),
        policy_hash="hrp-1",
        policy_generation=0,
        policy_json=None,
        reserved_at=reserved_at,
        reserved_by="someone",
        released_at=None,
        confirmed_at=None,
        confirmed_by=None,
    )


def _pressure(store: object, **overrides: object) -> ReviewPressure:
    kwargs: dict[str, object] = {
        "delivery_repo": _PRESSURE_REPO,
        "system_policy": _system_policy(),
        "now": _PRESSURE_NOW,
    }
    kwargs.update(overrides)
    return read_review_pressure(store, **kwargs)  # type: ignore[arg-type]


def test_a_never_reconciled_repository_applies_no_pressure() -> None:
    # Nothing has ever been observed, so this host has no normalized E5
    # obligation to be honest about. Silence is not saturation, and inventing
    # pressure from it would stop every driver on every host that has not yet
    # reconciled anything.
    pressure = _pressure(_FakeCapacityStore(observation=None))

    assert pressure.engaged is False
    assert pressure.saturated is False
    assert pressure.reason() == ""


def test_a_store_with_no_capacity_reads_applies_no_pressure() -> None:
    assert _pressure(object()).saturated is False


def test_below_the_cap_is_not_saturated() -> None:
    pressure = _pressure(_FakeCapacityStore(observation=_seen(), occupancy=2))

    assert pressure.countable is True
    assert (pressure.occupancy, pressure.cap) == (2, 3)
    assert pressure.saturated is False
    assert pressure.reason() == ""


def test_at_the_cap_is_saturated() -> None:
    pressure = _pressure(_FakeCapacityStore(observation=_seen(), occupancy=3))

    assert pressure.saturated is True
    assert "holding 3 of 3" in pressure.reason()


def test_over_the_cap_after_a_lowering_holds_and_evicts_nothing() -> None:
    # The override lowered the cap under three existing holds. Backpressure is
    # the only lever here: nothing already in front of a person is taken back.
    store = _FakeCapacityStore(
        observation=_seen(),
        effective=_system_policy(max_pending_reviews=1, generation=7),
        occupancy=3,
    )

    pressure = _pressure(store)

    assert (pressure.occupancy, pressure.cap) == (3, 1)
    assert pressure.policy_generation == 7
    assert pressure.saturated is True
    assert "holding 3 of 1" in pressure.reason()
    assert "no existing review was evicted" in pressure.reason()


def test_a_zero_cap_holds_everything_rather_than_disabling_the_check() -> None:
    store = _FakeCapacityStore(
        observation=_seen(), effective=_system_policy(max_pending_reviews=0)
    )

    assert _pressure(store).saturated is True


def test_an_unavailable_remote_never_becomes_zero() -> None:
    store = _FakeCapacityStore(
        observation=unavailable_review_observation(
            delivery_repo=_PRESSURE_REPO,
            generation=4,
            observed_at=_PRESSURE_NOW.isoformat(),
        )
    )

    pressure = _pressure(store)

    assert pressure.engaged is True
    assert pressure.countable is False
    assert pressure.occupancy == 0
    assert pressure.saturated is True
    assert "not an empty one" in pressure.reason()
    # The count is never even asked for: there is no observation to count from.
    assert store.occupancy_calls == []


def test_a_stale_observation_is_not_a_count() -> None:
    store = _FakeCapacityStore(
        observation=_seen(observed_at=(_PRESSURE_NOW - timedelta(hours=1)).isoformat()),
        occupancy=0,
    )

    pressure = _pressure(store)

    assert pressure.countable is False
    assert pressure.saturated is True


def test_another_repositorys_observation_is_not_this_ones() -> None:
    store = _FakeCapacityStore(observation=_seen(delivery_repo="o/other"))

    pressure = _pressure(store)

    assert pressure.countable is False
    assert pressure.saturated is True


def test_an_unreadable_persisted_observation_brakes_rather_than_frees() -> None:
    store = _FakeCapacityStore(
        raises=PublicationJobValueError("the persisted review observation is garbled")
    )

    pressure = _pressure(store)

    assert pressure.engaged is True
    assert pressure.saturated is True
    assert "unreadable" in pressure.reason()


def test_an_unexpected_observation_read_error_brakes_rather_than_crashing() -> None:
    # The capacity reads run raw SQL without the reservation path's busy-retry,
    # so a transient sqlite3.OperationalError is not a PublicationJobError.
    # "unknown -> assume saturated" must be total: the tick must brake, not die.
    store = _FakeCapacityStore(raises=sqlite3.OperationalError("database is locked"))

    pressure = _pressure(store)

    assert pressure.engaged is True
    assert pressure.countable is False
    assert pressure.saturated is True
    assert "unreadable" in pressure.reason()
    # An unreadable observation is never counted from.
    assert store.occupancy_calls == []


def test_an_unreadable_occupancy_count_brakes_rather_than_crashing() -> None:
    # The observation is countable, but the occupancy read itself blows up.
    # We must not crash the tick, and must not fall through as countable with a
    # zero occupancy (which would read as "capacity free" and flood).
    store = _FakeCapacityStore(
        observation=_seen(),
        occupancy_raises=sqlite3.OperationalError("database is locked"),
    )

    pressure = _pressure(store)

    assert store.occupancy_calls == [_PRESSURE_REPO]
    assert pressure.engaged is True
    assert pressure.countable is False
    assert pressure.occupancy == 0
    assert pressure.saturated is True
    assert "unreadable" in pressure.reason()


def test_an_epic_with_no_delivery_repository_has_no_capacity_to_read() -> None:
    store = _FakeCapacityStore(raises=ReviewPolicyError("there is no global count"))

    assert _pressure(store, delivery_repo="").engaged is False


def test_held_count_oldest_age_and_diff_burden_explain_the_number() -> None:
    store = _FakeCapacityStore(
        observation=_seen(),
        occupancy=2,
        reviews={
            "pj-1": _held_review(
                "pj-1",
                reserved_at=(_PRESSURE_NOW - timedelta(hours=5)).isoformat(),
                added=100,
                deleted=20,
            ),
            "pj-2": _held_review(
                "pj-2",
                reserved_at=(_PRESSURE_NOW - timedelta(hours=1)).isoformat(),
                added=30,
                deleted=0,
            ),
            # Released: it is no longer in front of anybody.
            "pj-3": _held_review("pj-3", state="released", added=999),
            # Another repository's queue is not this one's burden.
            "pj-4": _held_review("pj-4", delivery_repo="o/other", added=999),
        },
    )

    pressure = _pressure(store)

    assert pressure.held == 2
    assert pressure.oldest_hold_age_seconds == 5 * 3600
    assert pressure.diff_burden_lines == 150


def test_the_detail_is_reported_but_never_compared_against_the_cap() -> None:
    # Local holds explain the number; the authoritative union is the number.
    store = _FakeCapacityStore(
        observation=_seen(),
        occupancy=1,
        reviews={f"pj-{i}": _held_review(f"pj-{i}") for i in range(9)},
    )

    pressure = _pressure(store)

    assert pressure.held == 9
    assert pressure.saturated is False


# --- the same facts, read from the real store -------------------------------


def _real_store(tmp_path, name: str = "run.db"):
    return SqliteRunStore(str(tmp_path / name))


def _record_seen(store, *, refs: tuple[str, ...] = (), generation: int = 0) -> None:
    store.record_review_observation(
        RemoteReviewObservation(
            delivery_repo=_PRESSURE_REPO,
            availability=REVIEW_REMOTE_AVAILABLE,
            observed_at=_PRESSURE_NOW.isoformat(),
            generation=generation,
            obligations=tuple(ReviewObligation(ref=ref) for ref in refs),
        )
    )


def _held_job(store, *, run_id: str = "run-pub", reserved_at: datetime) -> str:
    """A real publication job holding a real review slot on the real store."""
    policy = ReviewPolicy(delivery_repo=_PRESSURE_REPO)
    base, tree = "b" * 40, "c" * 40
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=42,
            item_id="42",
            branch=f"agent/{run_id}",
            status="success",
            exit_code=0,
            started_at=_PRESSURE_NOW,
            finished_at=_PRESSURE_NOW,
        )
    )
    receipt = store.seal_artifact_receipt(
        lineage=ArtifactLineage(
            source_kind="github",
            source_id=_PRESSURE_REPO,
            run_id=run_id,
            attempt_number=1,
            base_sha=base,
            package_digest="sha256:" + "a" * 64,
            tree_sha=tree,
            verification=VERIFICATION_EXACT_TREE,
            locator="packages/" + "a" * 64,
        ),
        sealed_at=_PRESSURE_NOW,
    )
    job = store.open_publication_job(
        intent=PublicationIntent(
            source_kind="github",
            source_id=_PRESSURE_REPO,
            run_id=run_id,
            attempt_number=1,
            branch=f"agent/{run_id}",
            base_sha=base,
            tree_sha=tree,
            receipt_digest=receipt.receipt_digest,
            message="feat: the change",
        ),
        now=_PRESSURE_NOW,
    )
    risk = measure_risk(
        base_sha=base,
        tree_sha=tree,
        changes=[PathChange("src/a.py", added=900, deleted=100)],
    )
    store.record_publication_review(
        job_id=job.job_id,
        policy=policy,
        risk=risk,
        decision=decide_publication(policy=policy, risk=risk, pending_reviews=0),
        now=_PRESSURE_NOW,
    )
    store.reserve_publication_review(
        job_id=job.job_id, policy=policy, actor="operator", now=reserved_at
    )
    return job.job_id


def test_occupancy_is_the_authoritative_count_not_a_second_one(tmp_path) -> None:
    store = _real_store(tmp_path)
    try:
        _record_seen(store, refs=("https://github.com/o/r/pull/1", ".../pull/2"))
        observation = store.review_observation(_PRESSURE_REPO)

        pressure = _pressure(store)

        assert pressure.occupancy == store.review_capacity_occupancy(
            delivery_repo=_PRESSURE_REPO, observation=observation
        )
        assert pressure.occupancy == 2
        assert pressure.observation_generation == 0
        assert pressure.saturated is False
        assert (
            _pressure(
                store, system_policy=_system_policy(max_pending_reviews=2)
            ).saturated
            is True
        )
    finally:
        store.close()


def test_restart_reconstructs_the_same_pressure(tmp_path) -> None:
    store = _real_store(tmp_path)
    try:
        _record_seen(store, refs=("https://github.com/o/r/pull/1",))
        _held_job(store, reserved_at=_PRESSURE_NOW - timedelta(hours=2))
        before = _pressure(store)
    finally:
        store.close()

    restarted = _real_store(tmp_path)
    try:
        # Nothing was carried across in memory: every number is re-derived
        # from the same persisted facts, so a driver restart cannot forget a
        # queue it was already holding back for.
        assert _pressure(restarted) == before
        assert before.held == 1
        assert before.oldest_hold_age_seconds == 2 * 3600
        assert before.diff_burden_lines == 1000
    finally:
        restarted.close()


def test_an_expired_override_reverts_to_the_system_cap(tmp_path) -> None:
    store = _real_store(tmp_path)
    try:
        _record_seen(store, refs=("https://github.com/o/r/pull/1",))
        store.set_human_review_policy(
            policy=ReviewPolicy(
                delivery_repo=_PRESSURE_REPO,
                max_pending_reviews=1,
                generation=1,
                expires_at=(_PRESSURE_NOW - timedelta(hours=1)).isoformat(),
            ),
            actor="operator",
            now=_PRESSURE_NOW - timedelta(hours=2),
        )

        # While it applied, one obligation was already at the lowered cap.
        assert _pressure(store, now=_PRESSURE_NOW - timedelta(hours=2)).saturated

        # Expired: the effective policy is the system one again, under a new
        # generation, and the driver reads that rather than the stale override.
        after = _pressure(store)

        assert after.cap == 3
        assert after.saturated is False
        assert store.get_human_review_policy(_PRESSURE_REPO) is None
    finally:
        store.close()


class _MinimalCapacityStore:
    """Only the two reads a pressure decision cannot be made without."""

    def __init__(self, observation: RemoteReviewObservation, occupancy: int) -> None:
        self._observation = observation
        self._occupancy = occupancy

    def review_observation(self, delivery_repo: str) -> RemoteReviewObservation:
        return self._observation

    def review_capacity_occupancy(
        self, *, delivery_repo: str, observation: RemoteReviewObservation
    ) -> int:
        return self._occupancy


def test_a_store_without_policy_or_detail_reads_still_decides() -> None:
    # No override resolver and no local review rows: the system policy stands
    # and the detail is simply empty. The cap decision is still made.
    pressure = _pressure(_MinimalCapacityStore(_seen(), 3))

    assert pressure.saturated is True
    assert pressure.held == 0
    assert pressure.oldest_hold_age_seconds is None


def test_the_reason_names_the_oldest_hold_and_the_diff_waiting() -> None:
    store = _FakeCapacityStore(
        observation=_seen(),
        occupancy=3,
        reviews={
            "pj-1": _held_review(
                "pj-1",
                reserved_at=(_PRESSURE_NOW - timedelta(hours=2)).isoformat(),
                added=40,
                deleted=2,
            )
        },
    )

    reason = _pressure(store).reason()

    assert "1 local hold(s)" in reason
    assert "oldest 2.0h" in reason
    assert "42 diff line(s) waiting" in reason


def test_an_unusable_reservation_stamp_is_an_unknown_age_not_a_wrong_one() -> None:
    store = _FakeCapacityStore(
        observation=_seen(),
        occupancy=1,
        reviews={
            "pj-1": _held_review("pj-1", reserved_at="not-a-time"),
            "pj-2": _held_review("pj-2", reserved_at="2026-08-21T10:00:00"),
        },
    )

    pressure = _pressure(store)

    assert pressure.held == 2
    assert pressure.oldest_hold_age_seconds is None
    assert "oldest" not in pressure.reason()


# ---------------------------------------------------------------------------
# I12 / Task 49: the durable no-change projection the graph driver reads.
#
# The driver must never advance a dependency off a provider status shortcut.
# These tests pin what `read_no_change_facts` is allowed to say about one
# item: which judgment owns it, whether every authority actually
# ACKNOWLEDGED the synchronization, and which dependents (if any) hold an
# acknowledged dependency effect.
# ---------------------------------------------------------------------------


NC_BASE = "a" * 40
NC_DIGEST = "sha256:" + "b" * 64
NC_CHECK_DIGEST = "sha256:" + "c" * 64
NC_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _nc_evidence() -> NoChangeEvidence:
    return NoChangeEvidence(
        trusted_base_sha=NC_BASE,
        observed_base_sha=NC_BASE,
        package_digest=NC_DIGEST,
        package_scope="workspace",
        package_entry_count=0,
        package_deletion_count=0,
        checks=(NamedCheck(name="pytest", outcome="passed", digest=NC_CHECK_DIGEST),),
        attempted_changes=(),
    )


def _nc_authorized(store, *, item: str = "1", run_id: str = "run-1"):
    """A judgment for github item *item*, carried to ``authorized``."""
    intent = NoChangeIntent(
        source_kind="github",
        source_id=item,
        run_id=run_id,
        attempt_number=1,
        base_sha=NC_BASE,
        item_id=f"gh:{item}",
    )
    judgment = store.open_no_change_judgment(
        intent=intent, policy=CLOSE_IF_HOST_VERIFIED, now=NC_NOW
    )
    store.attest_no_change(
        judgment_id=judgment.judgment_id, evidence=_nc_evidence(), now=NC_NOW
    )
    return store.decide_no_change(
        judgment_id=judgment.judgment_id,
        contract=AlreadySatisfiedContract(
            contract_version=ALREADY_SATISFIED_CONTRACT_VERSION,
            item_id=f"gh:{item}",
            satisfied=True,
            producer_kind="host",
            verifier="kew.host_verifier",
            digest=NC_DIGEST,
        ),
        now=NC_NOW,
    )


def _nc_sync(store, judgment_id: str, *, item: str = "1"):
    return store.enqueue_outcome_sync(
        judgment_id=judgment_id, targets=(("github", item),), now=NC_NOW
    )[0]


def _nc_ack_sync(store, job) -> None:
    store.observe_outcome_sync(
        sync_job_id=job.sync_job_id,
        observation_kind="closed_by_kew",
        observation_ref="o/r#1",
        now=NC_NOW,
    )
    store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=NC_NOW)


def test_no_change_facts_are_keyed_by_item_and_carry_the_live_judgment(
    tmp_path,
) -> None:
    with SqliteRunStore(str(tmp_path / "nc.db")) as store:
        judgment = _nc_authorized(store)
        facts = read_no_change_facts(store, "github")
    assert set(facts) == {"1"}
    fact = facts["1"]
    assert fact.judgment_id == judgment.judgment_id
    assert fact.state == AUTHORIZED
    assert fact.live
    # Authorization alone is not an acknowledged synchronization, and it owes
    # no dependent anything yet.
    assert not fact.sync_acknowledged
    assert fact.acked_dependents == frozenset()


def test_no_change_facts_ignore_another_providers_lineage(tmp_path) -> None:
    with SqliteRunStore(str(tmp_path / "nc.db")) as store:
        _nc_authorized(store)
        assert read_no_change_facts(store, "linear") == {}


def test_an_observed_warning_is_not_an_acknowledged_no_change(tmp_path) -> None:
    """Looking at the tracker is not being told by it: only an ack counts."""
    with SqliteRunStore(str(tmp_path / "nc.db")) as store:
        judgment = _nc_authorized(store)
        job = _nc_sync(store, judgment.judgment_id)
        store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id, dependents=("2",), now=NC_NOW
        )
        store.observe_outcome_sync(
            sync_job_id=job.sync_job_id,
            observation_kind="conflicting_remote_state",
            observation_ref="o/r#1",
            now=NC_NOW,
        )
        fact = read_no_change_facts(store, "github")["1"]
    assert fact.state == SYNC_PENDING
    assert not fact.sync_acknowledged
    assert fact.acked_dependents == frozenset()


def test_only_acknowledged_dependency_effects_are_projected(tmp_path) -> None:
    with SqliteRunStore(str(tmp_path / "nc.db")) as store:
        judgment = _nc_authorized(store)
        job = _nc_sync(store, judgment.judgment_id)
        effects = store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id, dependents=("2", "3"), now=NC_NOW
        )
        _nc_ack_sync(store, job)
        store.begin_dependency_effect(effect_id=effects[0].effect_id, now=NC_NOW)
        applying = read_no_change_facts(store, "github")["1"]
        store.ack_dependency_effect(effect_id=effects[0].effect_id, now=NC_NOW)
        acked = read_no_change_facts(store, "github")["1"]
    assert applying.sync_acknowledged
    assert applying.state == EFFECT_PENDING
    # `applying` is a claim, never a grant.
    assert applying.acked_dependents == frozenset()
    assert acked.acked_dependents == frozenset({"2"})


def test_a_rejected_judgment_grants_no_dependency_effect(tmp_path) -> None:
    """Rejection revokes the outcome; an effect it already wrote is not a grant."""
    with SqliteRunStore(str(tmp_path / "nc.db")) as store:
        judgment = _nc_authorized(store)
        job = _nc_sync(store, judgment.judgment_id)
        effects = store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id, dependents=("2", "3"), now=NC_NOW
        )
        _nc_ack_sync(store, job)
        store.begin_dependency_effect(effect_id=effects[0].effect_id, now=NC_NOW)
        store.ack_dependency_effect(effect_id=effects[0].effect_id, now=NC_NOW)
        store.reject_no_change(
            judgment_id=judgment.judgment_id,
            actor_id="reviewer",
            reason="the absence was not the answer",
            now=NC_NOW,
        )
        fact = read_no_change_facts(store, "github")["1"]
    assert fact.state == REJECTED
    assert not fact.live
    assert fact.acked_dependents == frozenset()


def test_a_live_no_change_judgment_supersedes_a_settled_earlier_generation(
    tmp_path,
) -> None:
    with SqliteRunStore(str(tmp_path / "nc.db")) as store:
        first = _nc_authorized(store)
        store.reject_no_change(
            judgment_id=first.judgment_id,
            actor_id="reviewer",
            reason="not this time",
            now=NC_NOW,
        )
        second = _nc_authorized(store, run_id="run-2")
        fact = read_no_change_facts(store, "github")["1"]
    assert fact.judgment_id == second.judgment_id
    assert fact.generation == 2
    assert fact.live


# --- Node -> PR linkage order (W2.6/#1741) --------------------------------
#
# The PR body keyword is a convenience, never the key: a rewritten body drops
# `closedByPullRequestsReferences` and used to make a published node read TODO
# and get redispatched. Resolution order is ledger -> closing-ref -> branch.


def _linkage_run(store, *, item_id: str = "42", pr_url: str | None) -> None:
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=f"run-{item_id}",
            issue_num=int(item_id),
            item_id=item_id,
            branch=f"agent/issue-{item_id}",
            status="success",
            exit_code=0,
            started_at=now,
            finished_at=now,
            pr_url=pr_url,
        )
    )


def _linkage_run_fn(issue: dict, prs: list[dict], calls: list):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(json.dumps(issue))
        if is_pr_batch(cmd):
            return pr_batch_result(cmd, prs)
        if cmd[:3] == ["gh", "pr", "list"]:
            return _result(json.dumps(prs))
        if cmd[:3] == ["gh", "pr", "view"]:
            number = int(cmd[3])
            match = next((pr for pr in prs if pr["number"] == number), None)
            return _result(json.dumps(match) if match else "{}", 0 if match else 1)
        raise AssertionError(f"unexpected call: {cmd}")

    return fake_run


_OPEN_ISSUE_NO_KEYWORD = {
    "number": 42,
    "state": "OPEN",
    "stateReason": None,
    "labels": [],
    "closedByPullRequestsReferences": [],
}


def _pr(number: int, state: str, branch: str = "agent/issue-42") -> dict:
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "state": state,
        "headRefName": branch,
        "reviewDecision": None,
        "mergeStateStatus": "CLEAN",
        "headRefOid": "abc123",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }


def test_small_successful_batch_never_falls_back_across_three_ticks() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if is_pr_batch(cmd):
            return pr_batch_result(cmd, [])
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(json.dumps(_OPEN_ISSUE_NO_KEYWORD))
        raise AssertionError(f"unexpected call: {cmd}")

    reader = GitHubFactsReader("o/r", host="github.com", run=fake_run)
    for _ in range(3):
        reader.prepare(["42"])
        assert reader.issue_facts(42).pr_number is None

    assert [call for call in calls if "--head" in call] == []


def test_a_busy_repository_does_not_make_the_batch_fall_back() -> None:
    """#2582: the old batch listed the repository's newest 100 PRs, so in any
    repository with more it read as truncated and every node paid its own
    ``--head`` read. The batch now asks by branch, so a full repository is
    irrelevant: an empty answer for a branch is an answer."""
    others = [_pr(number, "OPEN", f"other/{number}") for number in range(1, 151)]
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if is_pr_batch(cmd):
            return pr_batch_result(cmd, others)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(json.dumps(_OPEN_ISSUE_NO_KEYWORD))
        raise AssertionError(f"unexpected call: {cmd}")

    reader = GitHubFactsReader("o/r", host="github.com", run=fake_run)
    reader.prepare(["42"])

    assert reader.issue_facts(42).pr_number is None
    assert [c[:3] for c in calls] == [["gh", "api", "graphql"], ["gh", "issue", "view"]]


@pytest.mark.parametrize("failure", ["exit", "garbage", "raises"])
def test_failed_batch_falls_back_for_a_missing_branch(failure: str) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(json.dumps(_OPEN_ISSUE_NO_KEYWORD))
        if is_pr_batch(cmd):
            if failure == "raises":
                raise OSError("gh vanished")
            if failure == "garbage":
                return _result("not json")
            return _result("", 1, "batch failed")
        if cmd[:3] == ["gh", "pr", "list"] and "--head" in cmd:
            return _result(json.dumps([_pr(777, "OPEN")]))
        raise AssertionError(f"unexpected call: {cmd}")

    reader = GitHubFactsReader("o/r", host="github.com", run=fake_run)
    reader.prepare(["42"])

    facts = reader.issue_facts(42)

    assert facts.pr_number == 777
    assert facts.pr_link_source == "branch"
    assert len([call for call in calls if "--head" in call]) == 1


def test_ledger_links_the_pr_when_the_body_keyword_is_gone(tmp_path) -> None:
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(_OPEN_ISSUE_NO_KEYWORD, [_pr(77, "OPEN")], calls),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_url == "https://github.com/o/r/pull/77"
    assert facts.pr_state == "OPEN"
    assert facts.pr_link_source == "ledger"
    assert facts.pr_has_closing_ref is False


def test_closing_ref_still_links_when_the_ledger_has_no_url(tmp_path) -> None:
    issue = dict(
        _OPEN_ISSUE_NO_KEYWORD,
        closedByPullRequestsReferences=[
            {"number": 77, "url": "https://github.com/o/r/pull/77"}
        ],
    )
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url=None)
        reader = GitHubFactsReader(
            "o/r", store=store, run=_linkage_run_fn(issue, [_pr(77, "OPEN")], calls)
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_link_source == "closing-ref"
    assert facts.pr_has_closing_ref is True


def test_ledger_winner_records_a_closing_reference_for_the_same_pr(tmp_path) -> None:
    issue = dict(
        _OPEN_ISSUE_NO_KEYWORD,
        closedByPullRequestsReferences=[
            {"number": 77, "url": "https://github.com/o/r/pull/77"}
        ],
    )
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader(
            "o/r", store=store, run=_linkage_run_fn(issue, [_pr(77, "OPEN")], calls)
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_link_source == "ledger"
    assert facts.pr_has_closing_ref is True


def test_branch_links_the_pr_with_no_ledger_url_and_no_closing_ref(tmp_path) -> None:
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url=None)
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(_OPEN_ISSUE_NO_KEYWORD, [_pr(77, "MERGED")], calls),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_state == "MERGED"
    assert facts.pr_link_source == "branch"


def test_branch_linkage_needs_no_run_row_at_all(tmp_path) -> None:
    """A node kew never dispatched locally still links by branch convention."""
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(_OPEN_ISSUE_NO_KEYWORD, [_pr(77, "OPEN")], calls),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_link_source == "branch"


def test_branch_linkage_honors_a_configured_branch_prefix(tmp_path) -> None:
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            branch_prefix="bot/task-",
            run=_linkage_run_fn(
                _OPEN_ISSUE_NO_KEYWORD, [_pr(77, "OPEN", "bot/task-42")], calls
            ),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_link_source == "branch"


def test_a_closed_unmerged_branch_pr_remains_linked(tmp_path) -> None:
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(_OPEN_ISSUE_NO_KEYWORD, [_pr(77, "CLOSED")], calls),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_state == "CLOSED"
    assert facts.pr_link_source == "branch"


def test_a_stale_ledger_url_falls_through_to_the_live_branch_pr(tmp_path) -> None:
    """A closed-unmerged first attempt must not mask the live second one."""
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/70")
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(
                _OPEN_ISSUE_NO_KEYWORD,
                [_pr(77, "OPEN"), _pr(70, "CLOSED")],
                calls,
            ),
        )
        for _ in range(3):
            reader.prepare(["42"])
            facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_link_source == "branch"


def test_an_unreadable_ledger_pr_still_links_rather_than_reading_as_no_pr(
    tmp_path,
) -> None:
    """An unreadable remote is not a successful "there is no PR"."""
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(json.dumps(_OPEN_ISSUE_NO_KEYWORD))
        if cmd[:3] == ["gh", "pr", "list"]:
            return _result("[]")
        return _result("", 1, "boom")

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader("o/r", store=store, run=fake_run)
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_state == "UNKNOWN"
    assert facts.pr_link_source == "ledger"


def test_an_open_branch_pr_outranks_a_merged_one_on_the_same_branch(
    tmp_path,
) -> None:
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(
                _OPEN_ISSUE_NO_KEYWORD,
                [_pr(70, "MERGED"), _pr(77, "OPEN")],
                calls,
            ),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77


def test_a_keywordless_pr_still_reads_in_review_end_to_end(tmp_path) -> None:
    """The whole point of #1741: strip the body keyword and the node must not
    fall back to a dispatchable TODO."""
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(_OPEN_ISSUE_NO_KEYWORD, [_pr(77, "OPEN")], calls),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    status = overlay_status(facts, [], reserved=False, held_reason=None)
    assert facts.pr_number == 77
    assert status is DriverStatus.IN_REVIEW


def test_a_keywordless_merged_pr_reads_held_end_to_end(tmp_path) -> None:
    """A squash-merge that closed nothing must not re-frontier the node."""
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(_OPEN_ISSUE_NO_KEYWORD, [_pr(77, "MERGED")], calls),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.state == "OPEN"
    assert overlay_status(facts, [], reserved=False, held_reason=None) is (
        DriverStatus.HELD
    )


def test_open_closing_ref_outranks_merged_ledger_and_logs_disagreement(
    tmp_path, caplog
) -> None:
    issue = dict(
        _OPEN_ISSUE_NO_KEYWORD,
        closedByPullRequestsReferences=[
            {"number": 77, "url": "https://github.com/o/r/pull/77"}
        ],
    )
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/70")
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            run=_linkage_run_fn(
                issue,
                [_pr(70, "MERGED"), _pr(77, "OPEN", "hand-made")],
                calls,
            ),
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert facts.pr_number == 77
    assert facts.pr_link_source == "closing-ref"
    assert "ledger=#70:MERGED" in caplog.text
    assert "closing-ref=#77:OPEN" in caplog.text
    assert caplog.text.count("linked PR disagreement") == 1


def _closed_snapshot(refs: list[dict]) -> dict:
    return {
        "number": 42,
        "state": "CLOSED",
        "stateReason": "COMPLETED",
        "labels": [],
        "closedByPullRequestsReferences": refs,
    }


def test_a_done_node_reads_its_merged_pr_from_the_snapshot_alone(tmp_path) -> None:
    """#2582: a closed issue whose snapshot states its PR is done — its PR is
    read off the snapshot, so a hold on it still sees the landed merge."""
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader(
            "o/r", store=store, host="github.com", run=_linkage_run_fn({}, [], calls)
        )
        reader.prime_issue_snapshots(
            {
                42: _closed_snapshot(
                    [
                        {
                            "number": 77,
                            "url": "https://github.com/o/r/pull/77",
                            "state": "MERGED",
                        }
                    ]
                )
            }
        )
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert calls == []
    assert (facts.pr_number, facts.pr_state) == (77, "MERGED")


def test_a_closed_node_whose_ledger_pr_is_unstated_is_still_asked(tmp_path) -> None:
    """A closed issue with no closing reference — kew merged a keywordless PR
    and closed it — must not read its PR as UNKNOWN: the batch asks for the
    ledger's number."""
    calls: list = []
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader(
            "o/r",
            store=store,
            host="github.com",
            run=_linkage_run_fn({}, [_pr(77, "MERGED")], calls),
        )
        reader.prime_issue_snapshots({42: _closed_snapshot([])})
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert [c[:3] for c in calls] == [["gh", "api", "graphql"]]
    assert batched_numbers(calls[0]) == [77]
    assert (facts.pr_number, facts.pr_state) == (77, "MERGED")


def test_a_deleted_ledger_pr_does_not_sink_the_batch_or_cost_a_read(tmp_path) -> None:
    """``gh api graphql`` exits 1 when one alias cannot resolve, yet prints the
    data it did resolve. The batch keeps that data, and the missing PR reads
    UNKNOWN without a per-node ``gh pr view`` to learn the same thing again."""
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if is_pr_batch(cmd):
            answered = pr_batch_payload(cmd, [_pr(78, "OPEN")])
            answered["errors"] = [{"message": "Could not resolve to a PullRequest"}]
            return _result(json.dumps(answered), 1, "gh: Could not resolve")
        if cmd[:3] == ["gh", "issue", "view"]:
            return _result(json.dumps(_OPEN_ISSUE_NO_KEYWORD))
        raise AssertionError(f"unexpected call: {cmd}")

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _linkage_run(store, pr_url="https://github.com/o/r/pull/77")
        reader = GitHubFactsReader("o/r", store=store, host="github.com", run=fake_run)
        reader.prepare(["42"])
        facts = reader.issue_facts(42)

    assert [c[:3] for c in calls] == [["gh", "api", "graphql"], ["gh", "issue", "view"]]
    assert facts.pr_number == 78
    assert facts.pr_link_source == "branch"
    assert reader.graphql_points == 2
