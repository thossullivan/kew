import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig
from kew.human_review import ReviewPolicyError
from kew.outcomes import (
    _ORPHAN_COMMENT_MARKER,
    OrphanPR,
    OrphanReconciliationResult,
    SyncResult,
    _QueryFailure,
    detect_orphan_prs,
    import_review_obligations,
    observe_review_obligations,
    reconcile_orphan_prs,
    sync_outcomes,
)
from kew.run_store import RunRecord, SqliteRunStore


def _run(run_id, branch, status="success", pr_state=None):
    r = RunRecord(
        run_id=run_id,
        issue_num=1,
        branch=branch,
        status=status,
        exit_code=0,
        started_at=datetime.now(UTC),
    )
    r.pr_state = pr_state
    return r


def _gh_payload(state="MERGED"):
    return json.dumps(
        {
            "url": "https://github.com/o/r/pull/7",
            "state": state,
            "mergedAt": "2026-06-24T10:00:00Z" if state == "MERGED" else None,
            "additions": 30,
            "deletions": 4,
            "changedFiles": 2,
            "reviews": [{"state": "COMMENTED"}, {"state": "APPROVED"}],
        }
    )


def test_sync_persists_all_outcome_fields():
    store = SqliteRunStore(":memory:")
    store.save(_run("r1", "agent/issue-1"))
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_gh_payload("MERGED"))
        n = sync_outcomes(store, config)
    assert n == SyncResult(synced=1)
    got = store.get("r1")
    assert got.pr_state == "MERGED"
    assert got.pr_url.endswith("/pull/7")
    assert got.review_cycles == 2  # len(reviews)
    assert got.diff_additions == 30 and got.diff_deletions == 4 and got.diff_files == 2


def test_sync_dedupes_gh_calls_by_branch():
    store = SqliteRunStore(":memory:")
    store.save(_run("r1", "agent/issue-1"))
    store.save(_run("r2", "agent/issue-1"))  # same branch
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_gh_payload())
        n = sync_outcomes(store, config)
    assert n == SyncResult(synced=2)  # both runs updated
    assert mock_run.call_count == 1  # one gh call for the shared branch


def test_sync_failsoft_on_gh_error_leaves_run_untouched():
    store = SqliteRunStore(":memory:")
    store.save(_run("r1", "agent/issue-1"))
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="authentication failed"
        )
        n = sync_outcomes(store, config)
    assert n == SyncResult(query_failures=1)
    assert store.get("r1").pr_state is None  # untouched, no raise


def test_sync_run_id_scopes_to_one_run():
    store = SqliteRunStore(":memory:")
    store.save(_run("r1", "agent/issue-1"))
    store.save(_run("r2", "agent/issue-2"))
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_gh_payload())
        n = sync_outcomes(store, config, run_id="r1")  # post-dispatch hot path
    assert n == SyncResult(synced=1)
    assert store.get("r1").pr_state == "MERGED"
    assert store.get("r2").pr_state is None  # not in scope


def test_kew_sync_command_reports_count(monkeypatch, tmp_path):
    # Pin cwd so load_config() can't walk up to the repo-root kew.toml (#633).
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)

    from typer.testing import CliRunner

    from kew.cli import app
    from kew.subprocess_utils import TOOL_PATHS
    from kew.subprocess_utils import run as _run

    # Make sure gh is in the subprocess allowlist
    TOOL_PATHS["gh"] = "/usr/bin/gh"
    try:
        # Mock the actual gh command call to avoid needing a real gh executable
        def mock_run(cmd, **kwargs):
            if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "view":
                # Mock successful repo detection
                class MockResult:
                    def __init__(self):
                        self.returncode = 0
                        self.stdout = "test/repo"
                        self.stderr = ""

                return MockResult()
            # For other commands, let them pass through
            return _run(cmd, **kwargs)

        monkeypatch.setattr(
            "kew.cli.sync_outcomes", lambda store, config: SyncResult(synced=4)
        )
        monkeypatch.setattr(
            "kew.cli.reconcile_orphan_prs", lambda *args: OrphanReconciliationResult()
        )
        monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
        monkeypatch.setattr("kew.cli.run", mock_run)
        # point config at a temp logging dir so the store opens cleanly
        from kew.config import DispatchConfig

        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("kew.cli.load_config", lambda: cfg)
        result = CliRunner().invoke(app, ["report", "sync"])
        assert result.exit_code == 0
        assert "4" in result.output
    finally:
        # Clean up the TOOL_PATHS
        TOOL_PATHS.pop("gh", None)


def _prs(*prs):
    """gh pr list --json ... payload."""
    return json.dumps(list(prs))


def _pr(number, branch):
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "headRefName": branch,
    }


class TestDetectOrphanPrs:
    """Orphan detection: open agent PRs with no successful run behind them (#648)."""

    def test_flags_failed_run_and_no_record(self):
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="failure"))
        store.save(_run("r2", "agent/issue-2", status="success"))
        config = DispatchConfig()
        config.project.repo = "o/r"
        payload = _prs(
            _pr(10, "agent/issue-1"),  # latest run failed -> orphan
            _pr(11, "agent/issue-2"),  # successful run -> NOT orphan
            _pr(12, "agent/issue-3"),  # no run record -> orphan
            _pr(13, "hotfix/manual"),  # not prefixed -> ignored
        )
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload)
            orphans = detect_orphan_prs(store, config)
        by_num = {o.number: o for o in orphans}
        assert set(by_num) == {10, 12}
        assert by_num[10].reason == "failed-run"
        assert by_num[10].branch == "agent/issue-1"
        assert by_num[12].reason == "no-record"

    def test_review_rows_do_not_stand_behind_or_orphan_a_pr(self):
        # A review works on the PR; only the implementation run's status counts
        # (#1923 round 2: review rows now name their branch).
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="success"))
        for kind in (
            "review",
            "review_remediation",
            "adversarial_review",
            "ci_fix",
        ):
            failed = _run(f"{kind}-later", "agent/issue-1", status="failure")
            failed.run_kind = kind
            store.save(failed)
        store.save(_run("r2", "agent/issue-2", status="failure"))
        passed = _run("review-2", "agent/issue-2", status="success")
        passed.run_kind = "review_remediation"
        store.save(passed)
        config = DispatchConfig()
        config.project.repo = "o/r"
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=_prs(_pr(10, "agent/issue-1"), _pr(11, "agent/issue-2")),
            )
            orphans = detect_orphan_prs(store, config)
        assert [(o.number, o.reason) for o in orphans] == [(11, "failed-run")]

    def test_latest_run_wins_over_earlier_success(self):
        # An earlier success followed by a later failed re-run is still an orphan.
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="success"))
        store.save(_run("r2", "agent/issue-1", status="failure"))  # newer
        config = DispatchConfig()
        config.project.repo = "o/r"
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=_prs(_pr(10, "agent/issue-1"))
            )
            orphans = detect_orphan_prs(store, config)
        assert [o.number for o in orphans] == [10]
        assert orphans[0].reason == "failed-run"

    def test_round15_no_repo_returns_failure(self):
        store = SqliteRunStore(":memory:")
        config = DispatchConfig()  # no repo configured
        assert detect_orphan_prs(store, config) == _QueryFailure(
            "repository_unavailable"
        )

    def test_failsoft_on_gh_error(self):
        store = SqliteRunStore(":memory:")
        config = DispatchConfig()
        config.project.repo = "o/r"
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert detect_orphan_prs(store, config) == _QueryFailure(
                "github_query_failed"
            )

    def test_failsoft_on_malformed_json(self):
        store = SqliteRunStore(":memory:")
        config = DispatchConfig()
        config.project.repo = "o/r"
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json")
            assert detect_orphan_prs(store, config) == _QueryFailure("invalid_response")


class TestReconcileOrphanPrs:
    """Reconciliation posts one idempotent note per orphan; never auto-closes."""

    def test_comments_on_orphan_when_absent(self):
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="failure"))
        config = DispatchConfig()
        config.project.repo = "o/r"
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout=_prs(_pr(10, "agent/issue-1")))
            if cmd[:3] == ["gh", "pr", "view"]:  # no existing comment
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("kew.outcomes.run", side_effect=fake_run):
            orphans = reconcile_orphan_prs(store, config)

        assert [o.number for o in orphans.orphans] == [10]
        assert any(c[:3] == ["gh", "pr", "comment"] for c in calls)

    def test_skips_comment_when_already_present(self):
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="failure"))
        config = DispatchConfig()
        config.project.repo = "o/r"
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout=_prs(_pr(10, "agent/issue-1")))
            if cmd[:3] == ["gh", "pr", "view"]:  # marker already present
                return MagicMock(
                    returncode=0, stdout=f"earlier note: {_ORPHAN_COMMENT_MARKER}\n"
                )
            return MagicMock(returncode=0, stdout="")

        with patch("kew.outcomes.run", side_effect=fake_run):
            orphans = reconcile_orphan_prs(store, config)

        assert [o.number for o in orphans.orphans] == [10]  # still surfaced
        assert not any(c[:3] == ["gh", "pr", "comment"] for c in calls)  # not re-posted

    def test_no_comment_when_no_orphans(self):
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="success"))
        config = DispatchConfig()
        config.project.repo = "o/r"

        def fake_run(cmd, **kw):
            if cmd[:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout=_prs(_pr(10, "agent/issue-1")))
            raise AssertionError("no further gh calls expected without orphans")

        with patch("kew.outcomes.run", side_effect=fake_run):
            assert reconcile_orphan_prs(store, config) == OrphanReconciliationResult()


class TestReconcilePersistsOrphans:
    """reconcile_orphan_prs writes detected orphans and clears resolved ones."""

    def _fake_run(self, prs_payload):
        def fake_run(cmd, **kw):
            if cmd[:3] == ["gh", "pr", "list"]:
                return MagicMock(returncode=0, stdout=prs_payload)
            # pr view (comment idempotency) + pr comment
            return MagicMock(returncode=0, stdout="")

        return fake_run

    def test_detected_orphans_persist_to_table(self):
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="failure"))
        config = DispatchConfig()
        config.project.repo = "o/r"
        payload = _prs(_pr(10, "agent/issue-1"))
        with patch("kew.outcomes.run", side_effect=self._fake_run(payload)):
            reconcile_orphan_prs(store, config)
        rows = {o.pr_number: o for o in store.list_orphan_prs()}
        assert set(rows) == {10}
        assert rows[10].reason == "failed-run"
        assert rows[10].branch == "agent/issue-1"

    def test_resolved_orphan_is_cleared(self):
        store = SqliteRunStore(":memory:")
        store.save(_run("r1", "agent/issue-1", status="failure"))
        config = DispatchConfig()
        config.project.repo = "o/r"
        # First sweep: PR 10 is an orphan → persisted.
        with patch(
            "kew.outcomes.run",
            side_effect=self._fake_run(_prs(_pr(10, "agent/issue-1"))),
        ):
            reconcile_orphan_prs(store, config)
        assert {o.pr_number for o in store.list_orphan_prs()} == {10}
        # Branch gains a successful run → PR 10 no longer an orphan → cleared.
        store.save(_run("r2", "agent/issue-1", status="success"))
        with patch(
            "kew.outcomes.run",
            side_effect=self._fake_run(_prs(_pr(10, "agent/issue-1"))),
        ):
            reconcile_orphan_prs(store, config)
        assert store.list_orphan_prs() == []


def test_sync_outcomes_orphan_pass_does_not_change_dedup_count():
    """Regression: the orphan work lives outside sync_outcomes' gh dedup path."""
    store = SqliteRunStore(":memory:")
    store.save(_run("r1", "agent/issue-1"))
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({"url": "u"}))
        sync_outcomes(store, config)
    # sync_outcomes itself makes exactly one gh call for the single branch.
    assert mock_run.call_count == 1


def test_kew_sync_reports_orphans(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from typer.testing import CliRunner

    from kew.cli import app

    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path / "logs")
    monkeypatch.setattr("kew.cli.load_config", lambda: cfg)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda config: None)
    monkeypatch.setattr(
        "kew.cli.sync_outcomes", lambda store, config: SyncResult(synced=2)
    )
    monkeypatch.setattr(
        "kew.cli.reconcile_orphan_prs",
        lambda store, config: OrphanReconciliationResult(
            orphans=(
                OrphanPR(
                    number=6, url="u", branch="agent/issue-6", reason="failed-run"
                ),
            )
        ),
    )
    result = CliRunner().invoke(app, ["report", "sync"])
    assert result.exit_code == 0
    assert "orphaned PR" in result.output
    assert "#6" in result.output


def test_kew_status_shows_orphan_line(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from typer.testing import CliRunner

    from kew.cli import app

    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(logs)
    monkeypatch.setattr("kew.cli.load_config", lambda: cfg)
    monkeypatch.setattr("kew.cli._preflight", lambda config: None)
    monkeypatch.setattr(
        "kew.cli.run_with_backoff",
        lambda cmd, **kw: MagicMock(returncode=0, stdout="0"),
    )
    monkeypatch.setattr(
        "kew.cli.detect_orphan_prs",
        lambda store, config: [
            OrphanPR(number=6, url="u", branch="agent/issue-6", reason="failed-run")
        ],
    )
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "orphaned PR" in result.output
    assert "#6" in result.output


# ---------------------------------------------------------------------------
# I14: reconciling the review obligations a delivery repository carries.
#
# The rest of this module fails soft into "nothing to do". These two functions
# must fail soft into "we do not know", because here an empty list is what
# frees a human-review slot.
# ---------------------------------------------------------------------------


def _pr_list_payload():
    return json.dumps(
        [
            {
                "number": 1,
                "url": "https://github.com/o/r/pull/1",
                "headRefName": "agent/issue-7",
                "body": "feat\n\n<!-- kew-publication-job: pj-0123456789abcdef -->",
                "state": "OPEN",
            },
            {
                "number": 2,
                "url": "https://github.com/o/r/pull/2",
                "headRefName": "someone/else",
                "body": "a colleague's pull request",
                "state": "OPEN",
            },
        ]
    )


def _repo_config():
    config = DispatchConfig()
    config.project.repo = "o/r"
    return config


class TestObservingReviewObligations:
    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def test_open_pull_requests_become_an_available_observation(self):
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_pr_list_payload())
            observation = observe_review_obligations(
                _repo_config(), generation=3, now=self._NOW
            )

        observation.validate()
        assert observation.available
        assert observation.generation == 3
        assert observation.delivery_repo == "o/r"
        assert [o.job_id for o in observation.obligations] == [
            "pj-0123456789abcdef",
            None,
        ]

    def test_a_pull_request_kew_did_not_open_is_still_an_obligation(self):
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_pr_list_payload())
            observation = observe_review_obligations(
                _repo_config(), generation=0, now=self._NOW
            )

        assert len(observation.obligations) == 2

    def test_an_empty_review_queue_is_available_and_empty(self):
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            observation = observe_review_obligations(
                _repo_config(), generation=0, now=self._NOW
            )

        assert observation.available
        assert observation.obligations == ()

    def test_a_gh_failure_is_unavailable_never_an_empty_queue(self):
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            observation = observe_review_obligations(
                _repo_config(), generation=0, now=self._NOW
            )

        observation.validate()
        assert not observation.available
        assert observation.obligations == ()

    def test_unreadable_gh_output_is_unavailable_never_an_empty_queue(self):
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json")
            observation = observe_review_obligations(
                _repo_config(), generation=0, now=self._NOW
            )

        assert not observation.available

    def test_a_project_that_names_no_delivery_repository_is_refused(self):
        with pytest.raises(ReviewPolicyError, match="names none"):
            observe_review_obligations(DispatchConfig(), generation=0, now=self._NOW)

    def test_importing_persists_the_observation_and_advances_the_generation(self):
        store = SqliteRunStore(":memory:")
        config = _repo_config()
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_pr_list_payload())
            first = import_review_obligations(store, config, now=self._NOW)
            second = import_review_obligations(store, config, now=self._NOW)

        assert (first.generation, second.generation) == (0, 1)
        assert store.review_observation("o/r") == second

    def test_an_outage_overwrites_stale_truth_rather_than_leaving_it_standing(self):
        store = SqliteRunStore(":memory:")
        config = _repo_config()
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_pr_list_payload())
            import_review_obligations(store, config, now=self._NOW)
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            outage = import_review_obligations(store, config, now=self._NOW)

        assert not outage.available
        assert not store.review_observation("o/r").available


# ---------------------------------------------------------------------------
# I11 (Tasks 46-48): authority, revalidation, and the outcome-sync reconciler.
#
# The reconciler is exercised through a fake adapter rather than a mocked `gh`
# in most cases: what is under test is the SEQUENCE (look, revalidate, write
# once, acknowledge) and what each unexpected answer does to it. The two
# adapter dialects get their own parity test at the foot of the file.
# ---------------------------------------------------------------------------

from kew.no_change import (  # noqa: E402
    AUTHORITY_RULE_ALLOWLIST,
    AUTHORITY_RULE_REPOSITORY,
    ITEM_CLOSED,
    ITEM_OPEN,
    OBSERVED_ALREADY_SYNCHRONIZED,
    OBSERVED_APPLIED,
    OBSERVED_CONFLICT,
    OBSERVED_STALE_SOURCE,
    OBSERVED_UNAVAILABLE,
    OBSERVED_WRITE_UNCONFIRMED,
    SYNC_ACKED,
    SYNC_ACTION_ACK,
    SYNC_ACTION_HOLD,
    SYNC_ACTION_MUTATE,
    SYNC_INTENT,
    SYNC_OBSERVED,
    SYNC_PENDING,
    ItemObservation,
    NamedCheck,
    NoChangeEvidence,
    NoChangeIntent,
    RepositoryAuthority,
    no_change_marker,
    plan_outcome_sync,
    resolve_no_change_authority,
    sync_idempotency_key,
)
from kew.outcomes import (  # noqa: E402
    no_change_item_adapter,
    observe_forge_identity,
    observe_repository_authority,
    post_no_change_rejection,
    revalidate_no_change_source,
    synchronize_no_change,
)

BASE = "a" * 40
MOVED = "b" * 40
PKG_DIGEST = "sha256:" + "c" * 64
CHECK_DIGEST = "sha256:" + "d" * 64
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _authority(**over):
    fields = {
        "delivery_repo": "o/r",
        "actor": "ada",
        "permission": "write",
        "observed_at": NOW.isoformat(),
    }
    fields.update(over)
    return RepositoryAuthority(**fields)


class TestNoChangeAuthority:
    """The exact split: an allowlist beats permission; no allowlist demands it."""

    def test_authority_allowlist_member_may_decide(self):
        grant = resolve_no_change_authority(
            actor="ada", approvers=("ada", "bob"), delivery_repo="o/r", now=NOW
        )
        assert grant.authorized
        assert grant.rule == AUTHORITY_RULE_ALLOWLIST
        assert grant.decided_at == NOW.isoformat()

    def test_authority_refuses_repo_admin_absent_from_a_named_allowlist(self):
        grant = resolve_no_change_authority(
            actor="root",
            approvers=("ada",),
            delivery_repo="o/r",
            authority=_authority(actor="root", permission="admin"),
            now=NOW,
        )
        assert not grant.authorized
        assert "not in the human_review allowlist" in grant.reason

    def test_authority_empty_allowlist_requires_fresh_repository_evidence(self):
        grant = resolve_no_change_authority(
            actor="ada", approvers=(), delivery_repo="o/r", authority=None, now=NOW
        )
        assert not grant.authorized
        assert grant.rule == AUTHORITY_RULE_REPOSITORY
        assert "not write access" in grant.reason

    @pytest.mark.parametrize("permission", ["write", "maintain", "admin"])
    def test_authority_accepts_each_writing_permission(self, permission):
        grant = resolve_no_change_authority(
            actor="ada",
            approvers=(),
            delivery_repo="o/r",
            authority=_authority(permission=permission),
            now=NOW,
        )
        assert grant.authorized
        assert grant.evidence_digest.startswith("sha256:")

    @pytest.mark.parametrize("permission", ["read", "triage", "none", ""])
    def test_authority_refuses_a_non_writing_permission(self, permission):
        grant = resolve_no_change_authority(
            actor="ada",
            approvers=(),
            delivery_repo="o/r",
            authority=_authority(permission=permission),
            now=NOW,
        )
        assert not grant.authorized

    def test_authority_refuses_unavailable_permission_evidence(self):
        grant = resolve_no_change_authority(
            actor="ada",
            approvers=(),
            delivery_repo="o/r",
            authority=_authority(available=False, permission="", detail="gh died"),
            now=NOW,
        )
        assert not grant.authorized
        assert "gh died" in grant.reason

    def test_authority_refuses_stale_permission_evidence(self):
        stale = _authority(observed_at=(NOW - timedelta(hours=4)).isoformat())
        grant = resolve_no_change_authority(
            actor="ada", approvers=(), delivery_repo="o/r", authority=stale, now=NOW
        )
        assert not grant.authorized
        assert "not fresh" in grant.reason

    def test_authority_refuses_evidence_about_another_actor_or_repo(self):
        other = _authority(actor="bob")
        grant = resolve_no_change_authority(
            actor="ada", approvers=(), delivery_repo="o/r", authority=other, now=NOW
        )
        assert not grant.authorized
        assert "describes 'bob'" in grant.reason

    def test_authority_refuses_an_unauthenticated_actor(self):
        grant = resolve_no_change_authority(
            actor="  ", approvers=(), delivery_repo="o/r", now=NOW
        )
        assert not grant.authorized
        assert "unauthenticated" in grant.reason

    def test_authority_rejects_an_author_who_satisfies_neither_rule(self):
        grant = resolve_no_change_authority(
            actor="ada",
            approvers=("bob",),
            delivery_repo="o/r",
            item_author="ada",
            now=NOW,
        )
        assert not grant.authorized
        assert "authorship of the item is not authority" in grant.reason

    def test_authority_allows_an_author_who_satisfies_the_active_rule(self):
        named = resolve_no_change_authority(
            actor="ada",
            approvers=("ada",),
            delivery_repo="o/r",
            item_author="ada",
            now=NOW,
        )
        by_permission = resolve_no_change_authority(
            actor="ada",
            approvers=(),
            delivery_repo="o/r",
            authority=_authority(),
            item_author="ada",
            now=NOW,
        )
        assert named.authorized and by_permission.authorized

    def test_authority_evidence_is_read_from_the_delivery_repository(self):
        config = DispatchConfig()
        config.project.repo = "o/r"
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="maintain\n")
            observed = observe_repository_authority(config, actor="ada", now=NOW)
        assert observed.available and observed.permission == "maintain"
        assert observed.observed_at == NOW.isoformat()
        assert "collaborators/ada/permission" in " ".join(mock_run.call_args[0][0])

    def test_authority_evidence_is_unavailable_when_gh_fails(self):
        config = DispatchConfig()
        config.project.repo = "o/r"
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="nope")
            observed = observe_repository_authority(config, actor="ada", now=NOW)
        assert not observed.available and observed.permission == ""

    def test_the_forge_identity_is_the_authenticated_gh_login(self):
        """#2463: the forge grants permission to a login, not to an OS user."""
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="thossullivan\n")
            identity = observe_forge_identity()
        assert identity.available and identity.login == "thossullivan"
        assert " ".join(mock_run.call_args[0][0]).startswith("gh api user")

    @pytest.mark.parametrize(
        ("returncode", "stdout"),
        [(1, ""), (0, ""), (0, "   \n")],
        ids=["gh-failed", "no-login", "blank-login"],
    )
    def test_an_unnamed_forge_login_is_unavailable_not_empty_string(
        self, returncode, stdout
    ):
        """An unread login must refuse, never read as "nobody in particular"."""
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=returncode, stdout=stdout, stderr="not logged in"
            )
            identity = observe_forge_identity()
        assert not identity.available and identity.login == ""
        assert identity.detail


# ---- the reconciler -------------------------------------------------------


class FakeAdapter:
    """A tracker that answers a scripted observation and records its writes."""

    def __init__(self, observation, *, confirms=True):
        self.observation = observation
        self.confirms = confirms
        self.writes = []
        self.looks = []

    def observe(self, *, marker):
        self.looks.append(marker)
        return self.observation

    def apply(self, *, body, close):
        self.writes.append({"body": body, "close": close})
        if self.confirms:
            self.observation = ItemObservation(
                available=True, state=ITEM_CLOSED, marker_found=True
            )
        return self.confirms


def _open_item(**over):
    fields = {"available": True, "state": ITEM_OPEN, "marker_found": False}
    fields.update(over)
    return ItemObservation(**fields)


def _no_change_config(repo="o/r"):
    config = DispatchConfig()
    config.project.repo = repo
    config.branches.base = "main"
    return config


def _evidence():
    return NoChangeEvidence(
        trusted_base_sha=BASE,
        observed_base_sha=BASE,
        package_digest=PKG_DIGEST,
        package_scope="workspace",
        package_entry_count=0,
        package_deletion_count=0,
        checks=(NamedCheck(name="pytest", outcome="passed", digest=CHECK_DIGEST),),
    )


def _sync_pending_judgment(store, *, source_kind="github", source_id="1533"):
    """A judgment carried by a person's confirmation to ``sync_pending``."""
    judgment = store.open_no_change_judgment(
        intent=NoChangeIntent(
            source_kind=source_kind,
            source_id=source_id,
            run_id="run-" + source_id,
            attempt_number=1,
            base_sha=BASE,
            item_id=source_id,
        ),
        now=NOW,
    )
    store.attest_no_change(
        judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
    )
    store.decide_no_change(judgment_id=judgment.judgment_id, now=NOW)
    store.confirm_no_change(judgment_id=judgment.judgment_id, actor_id="ada", now=NOW)
    store.enqueue_outcome_sync(
        judgment_id=judgment.judgment_id,
        targets=((source_kind, source_id),),
        now=NOW,
    )
    return store.get_no_change_judgment(judgment.judgment_id)


@pytest.fixture
def base_unmoved():
    """Every base revalidation in the block below reads the judged base."""
    with patch("kew.outcomes.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=BASE + "\n")
        yield mock_run


class TestPlanOutcomeSync:
    def test_outcome_sync_plan_writes_only_to_an_open_unmarked_item(self):
        assert plan_outcome_sync(_open_item()).action == SYNC_ACTION_MUTATE

    def test_outcome_sync_plan_acknowledges_its_own_marker_on_a_closed_item(self):
        plan = plan_outcome_sync(_open_item(state=ITEM_CLOSED, marker_found=True))
        assert plan.action == SYNC_ACTION_ACK
        assert plan.observation_kind == OBSERVED_ALREADY_SYNCHRONIZED

    def test_outcome_sync_plan_finishes_a_closure_its_marker_started(self):
        """Marked but still open: an earlier pass died between the two writes."""
        plan = plan_outcome_sync(_open_item(marker_found=True))
        assert plan.action == SYNC_ACTION_MUTATE
        assert plan.needs_comment is False

    def test_outcome_sync_plan_holds_on_an_unreadable_tracker(self):
        plan = plan_outcome_sync(ItemObservation(available=False, detail="502"))
        assert plan.action == SYNC_ACTION_HOLD
        assert plan.observation_kind == OBSERVED_UNAVAILABLE

    def test_outcome_sync_plan_holds_on_a_foreign_closure(self):
        plan = plan_outcome_sync(_open_item(state=ITEM_CLOSED))
        assert plan.action == SYNC_ACTION_HOLD
        assert plan.observation_kind == OBSERVED_CONFLICT


class TestSynchronizeNoChange:
    def test_no_change_sync_writes_once_and_acknowledges(self, tmp_path, base_unmoved):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item())

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=adapter,
        )

        assert [r.state for r in results] == [SYNC_ACKED]
        assert results[0].observation_kind == OBSERVED_APPLIED
        assert len(adapter.writes) == 1 and adapter.writes[0]["close"] is True
        job = store.list_outcome_sync_jobs(judgment.judgment_id)[0]
        assert job.state == SYNC_ACKED and job.acked_at
        # The judgment owes no dependent an effect yet, so the ack completes it.
        assert store.get_no_change_judgment(judgment.judgment_id).state == "completed"
        store.close()

    def test_no_change_sync_marker_is_stable_and_generation_bound(
        self, tmp_path, base_unmoved
    ):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item())
        synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=adapter,
        )
        expected = no_change_marker(
            sync_idempotency_key(
                judgment.judgment_id, judgment.generation, "github", "1533"
            )
        )
        assert adapter.looks == [expected]
        assert expected in adapter.writes[0]["body"]
        store.close()

    def test_no_change_sync_finishes_a_close_whose_comment_already_landed(
        self, tmp_path, base_unmoved
    ):
        """The comment landed and the close did not: finish it, do not re-note."""
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        half_written = FakeAdapter(_open_item(marker_found=True))

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=half_written,
        )

        assert [w["body"] for w in half_written.writes] == [None]
        assert half_written.writes[0]["close"] is True
        assert results[0].state == SYNC_ACKED
        store.close()

    def test_no_change_sync_holds_when_a_landed_comment_cannot_be_closed(
        self, tmp_path, base_unmoved
    ):
        """A half-written outcome is never acknowledged as a synchronized one."""
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        stuck = FakeAdapter(_open_item(marker_found=True), confirms=False)

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=stuck,
        )

        assert results[0].observation_kind == OBSERVED_WRITE_UNCONFIRMED
        assert results[0].state == SYNC_INTENT
        assert store.get_no_change_judgment(judgment.judgment_id).state == SYNC_PENDING
        store.close()

    def test_no_change_sync_replays_a_crash_after_the_remote_write(
        self, tmp_path, base_unmoved
    ):
        """The host died after commenting: the second pass discovers its own work."""
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        already = FakeAdapter(_open_item(state=ITEM_CLOSED, marker_found=True))

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=already,
        )

        assert already.writes == []  # nothing written a second time
        assert results[0].state == SYNC_ACKED
        assert results[0].observation_kind == OBSERVED_ALREADY_SYNCHRONIZED
        store.close()

    def test_no_change_sync_resumes_a_crash_between_observe_and_ack(
        self, tmp_path, base_unmoved
    ):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        job = store.list_outcome_sync_jobs(judgment.judgment_id)[0]
        store.observe_outcome_sync(
            sync_job_id=job.sync_job_id, observation_kind=OBSERVED_APPLIED, now=NOW
        )
        assert store.list_outcome_sync_jobs(judgment.judgment_id)[0].state == (
            SYNC_OBSERVED
        )
        adapter = FakeAdapter(_open_item(state=ITEM_CLOSED, marker_found=True))

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=adapter,
        )

        assert adapter.writes == []
        assert results[0].state == SYNC_ACKED
        store.close()

    def test_no_change_sync_is_idempotent_across_repeated_passes(
        self, tmp_path, base_unmoved
    ):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item())
        config = _no_change_config()
        for _ in range(3):
            synchronize_no_change(
                store,
                config,
                judgment_id=judgment.judgment_id,
                now=NOW,
                adapter=adapter,
            )
        assert len(adapter.writes) == 1
        store.close()

    def test_no_change_sync_holds_when_the_tracker_is_unreadable(
        self, tmp_path, base_unmoved
    ):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(ItemObservation(available=False, detail="tracker 503"))

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=adapter,
        )

        assert adapter.writes == []
        assert results[0].state == SYNC_INTENT
        assert results[0].observation_kind == OBSERVED_UNAVAILABLE
        assert store.list_outcome_sync_jobs(judgment.judgment_id)[0].state == (
            SYNC_INTENT
        )
        assert store.get_no_change_judgment(judgment.judgment_id).state == SYNC_PENDING
        store.close()

    def test_no_change_sync_holds_on_conflicting_remote_state(
        self, tmp_path, base_unmoved
    ):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        # Somebody else closed the item; it carries no marker from this judgment.
        adapter = FakeAdapter(_open_item(state=ITEM_CLOSED))

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=adapter,
        )

        assert adapter.writes == []
        assert results[0].observation_kind == OBSERVED_CONFLICT
        assert store.list_outcome_sync_jobs(judgment.judgment_id)[0].state == (
            SYNC_INTENT
        )
        store.close()

    def test_no_change_sync_holds_when_the_base_moved(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item())
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=MOVED)
            results = synchronize_no_change(
                store,
                _no_change_config(),
                judgment_id=judgment.judgment_id,
                now=NOW,
                adapter=adapter,
            )
        assert adapter.writes == []
        assert results[0].observation_kind == OBSERVED_STALE_SOURCE
        assert store.list_outcome_sync_jobs(judgment.judgment_id)[0].state == (
            SYNC_INTENT
        )
        store.close()

    def test_no_change_sync_holds_when_the_base_cannot_be_read(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item())
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="502")
            results = synchronize_no_change(
                store,
                _no_change_config(),
                judgment_id=judgment.judgment_id,
                now=NOW,
                adapter=adapter,
            )
        assert adapter.writes == []
        assert results[0].observation_kind == OBSERVED_STALE_SOURCE
        store.close()

    def test_no_change_sync_does_not_acknowledge_an_unconfirmed_write(
        self, tmp_path, base_unmoved
    ):
        """A warning is not an acknowledgement: the write went unconfirmed."""
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item(), confirms=False)

        results = synchronize_no_change(
            store,
            _no_change_config(),
            judgment_id=judgment.judgment_id,
            now=NOW,
            adapter=adapter,
        )

        assert len(adapter.writes) == 1
        assert results[0].observation_kind == OBSERVED_WRITE_UNCONFIRMED
        assert results[0].state == SYNC_INTENT
        assert store.list_outcome_sync_jobs(judgment.judgment_id)[0].acked_at is None
        store.close()

    def test_no_change_sync_holds_when_no_adapter_can_be_built(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store, source_id="1533")
        results = synchronize_no_change(
            store,
            _no_change_config(repo=""),
            judgment_id=judgment.judgment_id,
            now=NOW,
        )
        assert results[0].observation_kind == OBSERVED_UNAVAILABLE
        assert results[0].state == SYNC_INTENT
        store.close()

    def test_no_change_sync_refuses_an_unknown_judgment(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        with pytest.raises(ValueError):
            synchronize_no_change(
                store, _no_change_config(), judgment_id="nc-nope", now=NOW
            )
        store.close()


class TestNoChangeAdapters:
    def test_no_change_adapter_dispatches_on_the_source_authority(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        github = _sync_pending_judgment(store, source_id="1533")
        assert no_change_item_adapter(_no_change_config(), github) is not None
        # A GitHub source id that is not an issue number has nothing to write to.
        assert no_change_item_adapter(_no_change_config(repo=""), github) is None
        store.close()

    def test_no_change_github_adapter_looks_and_writes_once(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store, source_id="1533")
        marker = no_change_marker(
            sync_idempotency_key(
                judgment.judgment_id, judgment.generation, "github", "1533"
            )
        )
        adapter = no_change_item_adapter(_no_change_config(), judgment)
        payload = json.dumps(
            {"state": "OPEN", "url": "https://x/issues/1533", "comments": []}
        )
        with patch("kew.labels.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            observation = adapter.observe(marker=marker)
        assert observation.available and observation.state == ITEM_OPEN
        assert not observation.marker_found
        with patch("kew.labels.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert adapter.apply(body="note " + marker, close=True) is True
        assert [c[0][0][1] for c in mock_run.call_args_list] == ["issue", "issue"]
        store.close()

    def test_no_change_github_adapter_closes_without_re_noting(self, tmp_path):
        """``body=None`` is a resumed pass: the note is already on the item."""
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store, source_id="1533")
        adapter = no_change_item_adapter(_no_change_config(), judgment)
        with patch("kew.labels.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert adapter.apply(body=None, close=True) is True
        assert [c[0][0][1:3] for c in mock_run.call_args_list] == [["issue", "close"]]
        store.close()

    def test_no_change_github_adapter_reports_an_unreadable_item(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store, source_id="1533")
        adapter = no_change_item_adapter(_no_change_config(), judgment)
        with patch("kew.labels.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            observation = adapter.observe(marker="m")
        assert not observation.available
        store.close()

    def test_no_change_github_adapter_write_is_unconfirmed_when_close_fails(
        self, tmp_path
    ):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store, source_id="1533")
        adapter = no_change_item_adapter(_no_change_config(), judgment)
        with patch("kew.labels.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),  # comment landed
                MagicMock(returncode=1, stdout="", stderr="warning"),  # close did not
            ]
            assert adapter.apply(body="note", close=True) is False
        store.close()


class TestNoChangeRejectionNote:
    def test_no_change_rejection_note_never_closes_the_item(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        adapter = FakeAdapter(_open_item())
        noted = post_no_change_rejection(
            _no_change_config(),
            judgment=judgment,
            reason="the checks never ran",
            actor="ada",
            adapter=adapter,
        )
        assert noted is True
        assert adapter.writes[0]["close"] is False
        assert "stays open and unsatisfied" in adapter.writes[0]["body"]
        store.close()

    def test_no_change_rejection_note_is_best_effort(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        assert (
            post_no_change_rejection(
                _no_change_config(repo=""),
                judgment=judgment,
                reason="no",
                actor="ada",
            )
            is False
        )
        store.close()


class TestRevalidateNoChangeSource:
    def test_no_change_source_is_valid_while_the_base_stands(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        with patch("kew.outcomes.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=BASE)
            result = revalidate_no_change_source(_no_change_config(), judgment=judgment)
        assert result.valid and result.observed_base_sha == BASE
        store.close()

    def test_no_change_source_without_a_delivery_repo_is_never_valid(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        judgment = _sync_pending_judgment(store)
        result = revalidate_no_change_source(
            _no_change_config(repo=""), judgment=judgment
        )
        assert not result.valid
        store.close()


class TestTrackerParity:
    """One contract, two dialects: the same sequence against GitHub and Linear.

    Both adapters are the real ones, reached through ``no_change_item_adapter``
    — only the transports are faked — so a divergence in what either dialect
    treats as "written and confirmed" fails here rather than in production on
    whichever tracker the project happens to use.
    """

    @staticmethod
    def _linear_config():
        from kew.config import LinearTrackerConfig, TrackerConfig

        config = DispatchConfig(
            tracker=TrackerConfig(type="linear", linear=LinearTrackerConfig(team="SFD"))
        )
        config.project.repo = "o/r"
        config.branches.base = "main"
        return config

    @staticmethod
    def _linear_transport(calls):
        states = [
            {"id": "st-todo", "name": "Todo", "type": "unstarted"},
            {"id": "st-done", "name": "Done", "type": "completed"},
        ]

        def transport(url, payload, headers):
            doc = json.loads(payload)
            calls.append(doc)
            query = doc["query"]
            if "IssueOutcome" in query:
                return _linear_ok(
                    {
                        "issue": {
                            "identifier": "SFD-9",
                            "url": "https://linear.app/x/SFD-9",
                            "state": {"type": "started"},
                            "comments": {"nodes": []},
                        }
                    }
                )
            if "teams(" in query:
                return _linear_ok(
                    {"teams": {"nodes": [{"id": "t", "states": {"nodes": states}}]}}
                )
            if "commentCreate" in query:
                return _linear_ok(
                    {"commentCreate": {"success": True, "comment": {"id": "c1"}}}
                )
            if "issueUpdate" in query:
                return _linear_ok({"issueUpdate": {"success": True, "issue": None}})
            raise AssertionError(f"unrouted query: {query}")

        return transport

    def test_no_change_outcome_sync_parity_across_github_and_linear(
        self, tmp_path, monkeypatch
    ):
        from kew.linear.client import LinearClient

        store = SqliteRunStore(str(tmp_path / "db.sqlite"))
        github = _sync_pending_judgment(store, source_kind="github", source_id="1533")
        linear = _sync_pending_judgment(store, source_kind="linear", source_id="SFD-9")

        gh_payload = json.dumps({"state": "OPEN", "url": "u", "comments": []})
        with patch("kew.outcomes.run") as base_read, patch("kew.labels.run") as gh:
            base_read.return_value = MagicMock(returncode=0, stdout=BASE, stderr="")
            gh.return_value = MagicMock(returncode=0, stdout=gh_payload, stderr="")
            gh_results = synchronize_no_change(
                store,
                _no_change_config(),
                judgment_id=github.judgment_id,
                now=NOW,
            )

        calls: list = []
        monkeypatch.setenv("LINEAR_API_KEY", "fake-test-key-not-a-secret")
        transport = self._linear_transport(calls)
        with (
            patch("kew.outcomes.run") as base_read,
            patch(
                "kew.linear.client.LinearClient",
                lambda key: LinearClient(
                    key, transport=transport, sleep=lambda _s: None
                ),
            ),
        ):
            base_read.return_value = MagicMock(returncode=0, stdout=BASE, stderr="")
            linear_results = synchronize_no_change(
                store,
                self._linear_config(),
                judgment_id=linear.judgment_id,
                now=NOW,
            )

        assert [r.state for r in gh_results] == [r.state for r in linear_results]
        assert [r.observation_kind for r in gh_results] == [
            r.observation_kind for r in linear_results
        ]
        assert gh_results[0].state == SYNC_ACKED
        # Each tracker was commented on once and closed once, in that order.
        assert [c[0][0][1:3] for c in gh.call_args_list] == [
            ["issue", "view"],
            ["issue", "comment"],
            ["issue", "close"],
        ]
        mutations = [d["query"] for d in calls if "mutation" in d["query"]]
        assert len(mutations) == 2
        assert "commentCreate" in mutations[0] and "issueUpdate" in mutations[1]
        store.close()


def _linear_ok(data):
    return 200, json.dumps({"data": data}).encode()


@pytest.mark.parametrize(
    "reply,category",
    [
        (OSError("network unavailable"), "query_unavailable"),
        (MagicMock(returncode=1, stderr="HTTP 401", stdout=""), "github_query_failed"),
        (
            MagicMock(
                returncode=1,
                stderr='no pull requests found for branch "other"',
                stdout="",
            ),
            "github_query_failed",
        ),
        (MagicMock(returncode=0, stdout="not json"), "invalid_response"),
        (MagicMock(returncode=0, stdout="[]"), "invalid_response"),
        (MagicMock(returncode=0, stdout="{}"), "invalid_response"),
        (
            MagicMock(
                returncode=0,
                stdout=_gh_payload().replace(
                    '"reviews": [', '"reviews": null, "ignored": ['
                ),
            ),
            "invalid_response",
        ),
    ],
)
def test_round14_query_failures_have_static_categories(reply, category):
    from kew.outcomes import _fetch_pr_outcome

    with patch("kew.outcomes.run") as gh:
        if isinstance(reply, Exception):
            gh.side_effect = reply
        else:
            gh.return_value = reply
        result = _fetch_pr_outcome("agent/1", "o/r")
    assert getattr(result, "category", None) == category


def test_round14_absent_pr_can_be_discovered_on_later_sync():
    store = SqliteRunStore(":memory:")
    store.save(_run("r1", "agent/1"))
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as gh:
        gh.return_value = MagicMock(
            returncode=1, stderr='no pull requests found for branch "agent/1"'
        )
        assert sync_outcomes(store, config) == SyncResult(no_pr=1)
        assert store.get("r1").pr_state == "ABSENT"
        gh.return_value = MagicMock(returncode=0, stdout=_gh_payload())
        assert sync_outcomes(store, config) == SyncResult(synced=1)
    assert store.get("r1").pr_state == "MERGED"
    store.close()


def test_2264_old_absence_is_not_requeried_but_a_recent_one_is():
    """A months-old ABSENT branch never gets a PR; re-asking GitHub about it on
    every sweep drained the GraphQL quota (#2264). A recent one still can."""
    from kew.outcomes import ABSENT_RECHECK_WINDOW

    store = SqliteRunStore(":memory:")
    now = datetime.now(UTC)
    old = _run("old", "agent/old", pr_state="ABSENT")
    old.started_at = now - ABSENT_RECHECK_WINDOW - timedelta(days=1)
    recent = _run("recent", "agent/recent", pr_state="ABSENT")
    recent.started_at = now - timedelta(days=1)
    store.save(old)
    store.save(recent)
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout=_gh_payload())
        assert sync_outcomes(store, config, now=now) == SyncResult(synced=1)
    queried = [call.args[0][3] for call in gh.call_args_list]
    assert queried == ["agent/recent"]
    assert store.get("old").pr_state == "ABSENT"
    assert store.get("recent").pr_state == "MERGED"
    store.close()


def test_2264_explicit_run_id_is_always_queried_even_when_old():
    from kew.outcomes import ABSENT_RECHECK_WINDOW

    store = SqliteRunStore(":memory:")
    now = datetime.now(UTC)
    old = _run("old", "agent/old", pr_state="ABSENT")
    old.started_at = now - ABSENT_RECHECK_WINDOW - timedelta(days=30)
    store.save(old)
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch("kew.outcomes.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout=_gh_payload())
        result = sync_outcomes(store, config, run_id="old", now=now)
    assert result == SyncResult(synced=1)
    gh.assert_called_once()
    store.close()


def test_round14_failed_query_is_deduplicated_and_preserves_open_outcomes():
    store = SqliteRunStore(":memory:")
    for run_id in ("r1", "r2"):
        store.save(_run(run_id, "agent/1", pr_state="OPEN"))
    config = DispatchConfig()
    config.project.repo = "o/r"
    with patch(
        "kew.outcomes.run", return_value=MagicMock(returncode=1, stderr="HTTP 401")
    ) as gh:
        assert sync_outcomes(store, config) == SyncResult(query_failures=1)
    gh.assert_called_once()
    assert store.get("r1").pr_state == store.get("r2").pr_state == "OPEN"
    store.close()
