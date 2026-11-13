"""Acceptance disputes and amendments: the implementer never edits (#2731)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kew.acceptance_author import (
    AcceptanceAuthorRefused,
    AuthorRequest,
    amend_by_hand,
    prepare_acceptance_contract,
)
from kew.acceptance_dispute import (
    AcceptanceDispute,
    pending_dispute,
    record_acceptance_dispute,
    refer_dispute_to_author,
)
from kew.audit import Actor
from kew.cli import app
from kew.config import DispatchConfig
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus, settled_status
from kew.run_store import SqliteRunStore
from kew.service.attention import epic_hold_attention
from kew.subprocess_utils import TOOL_PATHS
from kew.tracking import DispatchResult
from kew.triage.plan import plan_sha
from kew.triage.schema import AcceptanceCriterion, ItemPlan

EPIC = "epic:x"
ISSUE = 7
ITEM = str(ISSUE)
CONTRACT = f"kew/tests/acceptance/test_issue_{ISSUE}.py"
PLAN = ItemPlan(
    acceptance_criteria=[
        AcceptanceCriterion(id="AC1", statement="value is two", kind="test")
    ]
)
TESTS_V1 = "def test_ac1():\n    assert 1 == 3\n"
TESTS_V2 = "def test_ac1():\n    assert 1 == 2\n"
DISPUTE = AcceptanceDispute(
    criterion="AC1",
    test="test_ac1",
    problem="asserts 3 where the criterion says 2",
    evidence="AC1 reads 'value is two'",
    proposed_fix="assert the value is 2",
)

runner = CliRunner()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _commit(repo: Path, text: str, message: str) -> str:
    target = repo / CONTRACT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A node branch whose first commit is the version-1 contract."""
    git = shutil.which("git")
    assert git is not None
    TOOL_PATHS["git"] = git
    repo = tmp_path / "checkout"
    (repo / "kew").mkdir(parents=True)
    (repo / "kew" / "feature.py").write_text("VALUE = 1\n")
    _git(repo, "init", "-q", "-b", f"agent/issue-{ISSUE}")
    _git(repo, "config", "user.email", "kew@example.invalid")
    _git(repo, "config", "user.name", "kew")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _store_with_contract(db: Path, checkout: Path) -> SqliteRunStore:
    store = SqliteRunStore(str(db))
    commit = _commit(checkout, TESTS_V1, "contract")
    store.record_acceptance_contract(
        EPIC,
        ITEM,
        path=CONTRACT,
        sha256=_sha(TESTS_V1),
        author_run_id="author-run-1",
        commit=commit,
        plan_sha=plan_sha(PLAN),
        test_names=("test_ac1",),
    )
    return store


def _dispatch_ctx(dispatch_core: Any, store: Any, tmp_path: Path, failures: list):
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
        epic_ref=EPIC,
        provider="github",
        issue_num=ISSUE,
        issue_title="Value is two",
        issue_body="",
        complexity="simple",
        item_id=ITEM,
        branch=f"agent/issue-{ISSUE}",
        publication_base_sha="c" * 40,
        start_monotonic=0.0,
        refuse_rollback=lambda _reason: None,
        terminal_failure=lambda category, reason: failures.append(category),
        run_kind="dispatch",
    )


def _needs_human_result(tmp_path: Path, reason: str) -> DispatchResult:
    return DispatchResult(
        exit_code=0,
        issue_num=ISSUE,
        log_file=tmp_path / "log",
        run_id="impl-run-1",
        error_category="needs_human",
        subtype=reason,
    )


def _facts() -> NodeFacts:
    return NodeFacts(
        issue_num=ISSUE,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=None,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
    )


def test_ac1_a_dispute_in_the_result_is_needs_you_and_spends_no_review_round(
    tmp_path: Path, checkout: Path
) -> None:
    from kew import dispatch_core

    failures: list[str] = []
    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        ctx = _dispatch_ctx(dispatch_core, store, tmp_path, failures)
        reason = "acceptance-disputed " + DISPUTE.model_dump_json()

        dispatch_core._collect_result(
            ctx,
            result=_needs_human_result(tmp_path, reason),
            worktree_dir=checkout,
            run_sandboxed_path=False,
        )

        [hold] = store.list_epic_holds(EPIC)
        assert (hold.item_id, hold.reason) == (ITEM, "acceptance-disputed")
        assert settled_status(_facts(), hold.reason) is DriverStatus.HELD
        assert epic_hold_attention(hold) is not None
        assert failures == []
        assert store.review_round_count(ITEM) == 0
        assert pending_dispute(store, EPIC, ITEM) is not None
        assert pending_dispute(store, EPIC, ITEM).dispute == DISPUTE  # type: ignore[union-attr]
        [event] = store.list_audit_events(event="acceptance_disputed")
        assert event.payload["dispute"]["test"] == "test_ac1"


def test_a_plain_needs_human_stop_is_still_a_published_failure(
    tmp_path: Path, checkout: Path
) -> None:
    from kew import dispatch_core

    failures: list[str] = []
    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        ctx = _dispatch_ctx(dispatch_core, store, tmp_path, failures)
        dispatch_core._collect_result(
            ctx,
            result=_needs_human_result(tmp_path, "policy gate failed: x"),
            worktree_dir=checkout,
            run_sandboxed_path=False,
        )

        assert failures == ["needs_human"]
        assert store.list_epic_holds(EPIC) == []


def test_a_dispute_without_a_contract_is_no_dispute(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        recorded = record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )

        assert recorded is None
        assert store.list_epic_holds(EPIC) == []


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kew project whose ledger lives in ``tmp_path/logs``."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{tmp_path / "logs"}"\n'
    )
    monkeypatch.setenv("KEW_PROJECT", str(root))
    (tmp_path / "logs").mkdir()
    return root


def _invoke_amend(checkout: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "epic",
            "acceptance",
            "amend",
            EPIC,
            ITEM,
            "--reason",
            "AC1 says two",
            "--checkout",
            str(checkout),
            *extra,
        ],
    )


def test_ac2_amend_records_a_new_version_and_an_audit_event_with_both_digests(
    tmp_path: Path, checkout: Path, project: Path
) -> None:
    db = tmp_path / "logs" / "dispatch.db"
    with _store_with_contract(db, checkout) as store:
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )
    amended = _commit(checkout, TESTS_V2, "amend contract")

    result = _invoke_amend(checkout)

    assert result.exit_code == 0, result.output
    with SqliteRunStore(str(db)) as store:
        versions = store.list_acceptance_contracts(EPIC, ITEM)
        assert [(v.version, v.sha256, v.commit) for v in versions][1:] == [
            (2, _sha(TESTS_V2), amended)
        ]
        [event] = store.list_audit_events(event="acceptance_amended")
        payload = event.payload
        assert (payload["old_sha256"], payload["new_sha256"]) == (
            _sha(TESTS_V1),
            _sha(TESTS_V2),
        )
        assert payload["reason"] == "AC1 says two"
        assert event.actor_type == "human"
        assert payload["dispute"]["dispute"] == DISPUTE.model_dump()
        assert store.list_epic_holds(EPIC) == []
        assert [r.item_id for r in store.list_epic_retries(EPIC)] == [ITEM]
        assert pending_dispute(store, EPIC, ITEM) is None


def test_amend_with_the_file_unchanged_records_nothing(
    tmp_path: Path, checkout: Path, project: Path
) -> None:
    db = tmp_path / "logs" / "dispatch.db"
    _store_with_contract(db, checkout).close()

    result = _invoke_amend(checkout)

    assert result.exit_code == 2, result.output
    with SqliteRunStore(str(db)) as store:
        assert len(store.list_acceptance_contracts(EPIC, ITEM)) == 1
        assert store.list_audit_events(event="acceptance_amended") == []


def test_amend_author_refers_the_dispute_and_releases_the_hold(
    tmp_path: Path, checkout: Path, project: Path
) -> None:
    db = tmp_path / "logs" / "dispatch.db"
    with _store_with_contract(db, checkout) as store:
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )

    result = _invoke_amend(checkout, "--author")

    assert result.exit_code == 0, result.output
    with SqliteRunStore(str(db)) as store:
        assert store.list_epic_holds(EPIC) == []
        assert [r.item_id for r in store.list_epic_retries(EPIC)] == [ITEM]
        assert pending_dispute(store, EPIC, ITEM).referred  # type: ignore[union-attr]
        assert len(store.list_acceptance_contracts(EPIC, ITEM)) == 1


def test_ac3_an_author_step_amendment_records_the_dispute_it_answered(
    tmp_path: Path, checkout: Path
) -> None:
    requests: list[AuthorRequest] = []

    def author(request: AuthorRequest) -> str:
        requests.append(request)
        (request.worktree / CONTRACT).write_text(TESTS_V2)
        return "author-run-2"

    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )
        refer_dispute_to_author(
            store,
            epic_ref=EPIC,
            item_id=ITEM,
            reason="let the author decide",
            actor=Actor("human", "tom"),
        )

        row = prepare_acceptance_contract(
            store=store,
            epic_ref=EPIC,
            item_id=ITEM,
            issue_num=ISSUE,
            issue_body="value is two",
            plan=PLAN,
            worktree=checkout,
            implementer_model="claude-opus-5-5",
            routed_models=["claude-opus-5-5"],
            contained=False,
            launch=author,
            run_test=lambda _worktree, _node: "failed",
        )

        assert row is not None
        assert (row.version, row.sha256, row.author_run_id) == (
            2,
            _sha(TESTS_V2),
            "author-run-2",
        )
        assert row.commit == _git(checkout, "rev-parse", "HEAD")
        assert DISPUTE.problem in requests[0].prompt
        [event] = store.list_audit_events(event="acceptance_amended")
        payload = event.payload
        assert payload["dispute"]["dispute"] == DISPUTE.model_dump()
        assert payload["dispute"]["run_id"] == "impl-run-1"
        assert (payload["old_sha256"], payload["new_sha256"]) == (
            _sha(TESTS_V1),
            _sha(TESTS_V2),
        )
        assert (event.actor_type, event.actor_id) == ("agent", "author-run-2")
        assert pending_dispute(store, EPIC, ITEM) is None


def test_an_unreferred_dispute_does_not_rerun_the_author(
    tmp_path: Path, checkout: Path
) -> None:
    def author(_request: AuthorRequest) -> str:
        raise AssertionError("the author must not run")

    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )

        row = prepare_acceptance_contract(
            store=store,
            epic_ref=EPIC,
            item_id=ITEM,
            issue_num=ISSUE,
            issue_body="value is two",
            plan=PLAN,
            worktree=checkout,
            implementer_model="claude-opus-5-5",
            routed_models=["claude-opus-5-5"],
            contained=False,
            launch=author,
        )

        assert row is not None and row.version == 1


def _contract_text(checkout: Path) -> str:
    """Return the acceptance contract text in *checkout*."""
    return (checkout / CONTRACT).read_text()


def _rewrite_contract(worktree: Path, _node: str) -> str:
    (worktree / CONTRACT).write_text(TESTS_V1)
    return "failed"


def _plant_source(worktree: Path, _node: str) -> str:
    (worktree / "kew" / "planted.py").write_text("planted\n")
    return "failed"


def _red(_worktree: Path, _node: str) -> str:
    return "failed"


#: Stages a planted file and points a replace ref for the base at a commit
#: whose tree holds it, so through the replacement the staged file reads clean.
_REPLACE_BASE = (
    "echo planted > kew/planted.py && git add kew/planted.py && "
    'git replace HEAD "$(git commit-tree "$(git write-tree)" -m base)"'
)


@pytest.mark.parametrize(
    ("script", "run_test", "match"),
    [
        ("true", _rewrite_contract, "rewrote"),
        ("true", _plant_source, "kew/planted.py"),
        (_REPLACE_BASE, _red, "kew/planted.py"),
    ],
    ids=["red-run-rewrites-contract", "red-run-plants-file", "author-replaces-base"],
)
def test_an_author_step_amendment_gets_the_checkout_checks_a_first_contract_does(
    tmp_path: Path, checkout: Path, script: str, run_test: Any, match: str
) -> None:
    """Answering a dispute re-runs the author and the red-at-base run, so the
    same refusals apply: nothing is committed and the contract stays at v1."""

    def author(request: AuthorRequest) -> str:
        (request.worktree / CONTRACT).write_text(TESTS_V2)
        subprocess.run(["sh", "-c", script], cwd=request.worktree, check=True)
        return "author-run-2"

    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        head = _git(checkout, "rev-parse", "HEAD")
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )
        refer_dispute_to_author(
            store,
            epic_ref=EPIC,
            item_id=ITEM,
            reason="let the author decide",
            actor=Actor("human", "tom"),
        )

        with pytest.raises(AcceptanceAuthorRefused, match=match):
            prepare_acceptance_contract(
                store=store,
                epic_ref=EPIC,
                item_id=ITEM,
                issue_num=ISSUE,
                issue_body="value is two",
                plan=PLAN,
                worktree=checkout,
                implementer_model="claude-opus-5-5",
                routed_models=["claude-opus-5-5"],
                contained=False,
                launch=author,
                run_test=run_test,
            )

        assert _git(checkout, "--no-replace-objects", "rev-parse", "HEAD") == head
        current = store.current_acceptance_contract(EPIC, ITEM)
        assert current is not None and current.version == 1
        assert _contract_text(checkout) == TESTS_V1
        assert not (checkout / "kew" / "planted.py").exists()


def _fresh_base_clone(tmp_path: Path, checkout: Path, base: str) -> Path:
    """A redispatch's clone: the base's history only, no node-branch commit."""
    _git(checkout, "branch", "-f", "kew-base", base)
    fresh = tmp_path / "fresh"
    _git(
        tmp_path,
        "clone",
        "-q",
        "--no-local",
        "--single-branch",
        "--branch",
        "kew-base",
        str(checkout),
        str(fresh),
    )
    _git(fresh, "config", "user.email", "kew@example.invalid")
    _git(fresh, "config", "user.name", "kew")
    return fresh


def _prepare_on(store: Any, worktree: Path, author: Any) -> Any:
    return prepare_acceptance_contract(
        store=store,
        epic_ref=EPIC,
        item_id=ITEM,
        issue_num=ISSUE,
        issue_body="value is two",
        plan=PLAN,
        worktree=worktree,
        implementer_model="claude-opus-5-5",
        routed_models=["claude-opus-5-5"],
        contained=False,
        launch=author,
        run_test=lambda _worktree, _node: "failed",
    )


def _no_author(_request: AuthorRequest) -> str:
    raise AssertionError("the author must not run")


def test_a_hand_amendment_stays_bound_to_the_plan_and_is_restored(
    tmp_path: Path, checkout: Path
) -> None:
    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        _commit(checkout, TESTS_V2, "amend AC1")
        amended = amend_by_hand(
            store=store,
            epic_ref=EPIC,
            item_id=ITEM,
            checkout=checkout,
            reason="AC1 says two",
            actor=Actor("human", "tom"),
        )

        row = _prepare_on(store, checkout, _no_author)

        assert (amended.plan_sha, amended.test_names) == (
            plan_sha(PLAN),
            ("test_ac1",),
        )
        assert row is not None
        assert (row.version, row.sha256) == (2, _sha(TESTS_V2))


def test_a_hand_amendment_a_fresh_clone_cannot_reach_is_never_replaced(
    tmp_path: Path, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        _commit(checkout, TESTS_V2, "amend AC1")
        amended = amend_by_hand(
            store=store,
            epic_ref=EPIC,
            item_id=ITEM,
            checkout=checkout,
            reason="AC1 says two",
            actor=Actor("human", "tom"),
        )
        fresh = _fresh_base_clone(tmp_path, checkout, base)

        with pytest.raises(AcceptanceAuthorRefused, match="was amended"):
            _prepare_on(store, fresh, _no_author)

        assert store.current_acceptance_contract(EPIC, ITEM) == amended
        assert _git(fresh, "rev-parse", "HEAD") == base
        assert not (fresh / CONTRACT).exists()


def test_a_referred_dispute_a_fresh_clone_cannot_answer_stays_open(
    tmp_path: Path, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )
        refer_dispute_to_author(
            store,
            epic_ref=EPIC,
            item_id=ITEM,
            reason="let the author decide",
            actor=Actor("human", "tom"),
        )
        fresh = _fresh_base_clone(tmp_path, checkout, base)

        with pytest.raises(AcceptanceAuthorRefused, match="open acceptance dispute"):
            _prepare_on(store, fresh, _no_author)

        assert len(store.list_acceptance_contracts(EPIC, ITEM)) == 1
        assert store.list_audit_events(event="acceptance_amended") == []
        dispute = pending_dispute(store, EPIC, ITEM)
        assert dispute is not None and dispute.referred
        assert dispute.contract_version == 1


def test_a_new_plan_closes_a_referred_dispute_against_the_old_contract(
    tmp_path: Path, checkout: Path
) -> None:
    reworded = ItemPlan(
        acceptance_criteria=[
            AcceptanceCriterion(id="AC1", statement="value is exactly two", kind="test")
        ]
    )
    requests: list[AuthorRequest] = []

    def author(request: AuthorRequest) -> str:
        requests.append(request)
        (request.worktree / CONTRACT).write_text(TESTS_V2)
        return "author-run-2"

    def prepare() -> Any:
        return prepare_acceptance_contract(
            store=store,
            epic_ref=EPIC,
            item_id=ITEM,
            issue_num=ISSUE,
            issue_body="value is exactly two",
            plan=reworded,
            worktree=checkout,
            implementer_model="claude-opus-5-5",
            routed_models=["claude-opus-5-5"],
            contained=False,
            launch=author,
            run_test=lambda _worktree, _node: "failed",
        )

    with _store_with_contract(tmp_path / "runs.db", checkout) as store:
        record_acceptance_dispute(
            store, epic_ref=EPIC, item_id=ITEM, dispute=DISPUTE, run_id="impl-run-1"
        )
        refer_dispute_to_author(
            store,
            epic_ref=EPIC,
            item_id=ITEM,
            reason="let the author decide",
            actor=Actor("human", "tom"),
        )

        authored = prepare()
        redispatched = prepare()

        assert len(requests) == 1
        assert DISPUTE.problem not in requests[0].prompt
        assert authored is not None and authored.plan_sha == plan_sha(reworded)
        assert redispatched == authored
        assert pending_dispute(store, EPIC, ITEM) is None
        assert store.list_audit_events(event="acceptance_amended") == []
        [event] = store.list_audit_events(event="acceptance_dispute_superseded")
        assert event.payload["dispute"]["contract_version"] == 1
        assert event.payload["new_version"] == authored.version
        assert (event.actor_type, event.actor_id) == ("agent", "author-run-2")
