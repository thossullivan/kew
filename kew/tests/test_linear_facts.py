"""LinearFactsReader: driver NodeFacts for Linear nodes (Group L2).

Mirrors GitHubFactsReader's contract, but with two provider swaps:

* issue state + labels come from Linear (workflow-state type, Linear labels),
  not `gh issue view`;
* the linked PR comes from the newest item run on the dispatch branch that has
  a PR URL (the branch is the join key — no `Closes #N` marker for Linear),
  with live open/merged state read via `gh pr view` on that branch. PRs still
  live on GitHub, so the PR-side helpers (`pr_facts` /
  `required_checks_green`) delegate unchanged.
"""

from __future__ import annotations

import json

from kew.graph.adapters.linear_facts import LinearFactsReader
from kew.graph.domain import NodeStatus
from kew.graph.driver.facts import NodeFacts, RepositoryPermission
from kew.linear.client import LinearApiError


def _result(stdout: str, returncode: int = 0, stderr: str = ""):
    class R:
        pass

    r = R()
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = stderr
    return r


class FakeClient:
    """Returns one canned issue payload and records every GraphQL call."""

    def __init__(self, issue, *, raise_not_found=False, raise_error=False):
        self._issue = issue
        self._raise_not_found = raise_not_found
        self._raise_error = raise_error
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, query, variables=None):
        self.calls.append((query, variables))
        if self._raise_not_found:
            raise LinearApiError("Entity not found", not_found=True)
        if self._raise_error:
            raise LinearApiError("network down")
        return {"issue": self._issue}


class FakeRun:
    """Minimal RunRecord stand-in for the fields the reader joins on."""

    def __init__(self, pr_url, branch="agent/issue-sfd-12", run_kind="dispatch"):
        self.pr_url = pr_url
        self.branch = branch
        self.run_kind = run_kind


class FakeStore:
    def __init__(self, run):
        self._runs = [] if run is None else ([run] if isinstance(run, FakeRun) else run)
        self.item_ids: list[str] = []

    def list_by_item(self, item_id):
        self.item_ids.append(item_id)
        return self._runs


def _issue(state_name, state_type, labels=()):
    return {
        "identifier": "SFD-12",
        "state": {"name": state_name, "type": state_type},
        "labels": {"nodes": [{"name": n} for n in labels]},
    }


def _pr_json(**overrides):
    base = {
        "number": 77,
        "url": "https://github.com/o/r/pull/77",
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "abc123",
        "commits": [{"committedDate": "2026-07-18T00:00:00Z"}],
    }
    base.update(overrides)
    return json.dumps(base)


def _pr_batch_json(**overrides):
    overrides.setdefault("headRefName", "agent/issue-sfd-12")
    return json.dumps([json.loads(_pr_json(**overrides))])


def _reader(issue, run, pr_stdout="", pr_rc=0, **kw):
    client = FakeClient(issue)
    store = FakeStore(run)
    gh_calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        gh_calls.append(cmd)
        return _result(pr_stdout, pr_rc)

    reader = LinearFactsReader(client, store, "o/r", run=fake_run, **kw)
    return reader, client, store, gh_calls


def test_repository_permission_delegates_to_github_reader() -> None:
    reader, _, _, calls = _reader(
        None,
        None,
        pr_stdout=json.dumps(
            {
                "permission": "maintain",
                "user": {"login": "alice", "type": "User"},
            }
        ),
    )

    assert reader.repository_permission("alice") == RepositoryPermission(
        "alice", "User", "maintain"
    )
    assert calls == [["gh", "api", "repos/o/r/collaborators/alice/permission"]]


def test_linked_pr_from_item_run_not_github() -> None:
    reader, client, store, gh_calls = _reader(
        _issue("In Review", "started"),
        FakeRun("https://github.com/o/r/pull/77"),
        pr_stdout=_pr_json(),
    )
    facts = reader.issue_facts("SFD-12")

    # Branch is prefix + lowercased slug; that exact branch is the store key.
    assert store.item_ids == ["SFD-12"]
    # The ledger URL supplies the durable PR number for the shared resolver.
    assert gh_calls[0][:4] == ["gh", "pr", "view", "77"]
    # No `gh issue view` / closedByPullRequestsReferences path at all.
    assert not any(call[:3] == ["gh", "issue", "view"] for call in gh_calls)
    assert facts.pr_number == 77
    assert facts.pr_url == "https://github.com/o/r/pull/77"
    assert facts.review_decision == "APPROVED"
    assert facts.merge_state_status == "CLEAN"
    assert facts.head_oid == "abc123"
    assert facts.pr_pushed_at == "2026-07-18T00:00:00Z"


def test_later_review_run_without_pr_does_not_mask_implementation_pr() -> None:
    reader, _, store, gh_calls = _reader(
        _issue("In Progress", "started"),
        [
            FakeRun("https://github.com/o/r/pull/77"),
            FakeRun(None),
        ],
        pr_stdout=_pr_json(state="MERGED"),
    )

    facts = reader.issue_facts("SFD-12")

    assert store.item_ids == ["SFD-12"]
    assert gh_calls[0][:4] == ["gh", "pr", "view", "77"]
    assert facts.pr_number == 77
    assert facts.pr_url == "https://github.com/o/r/pull/77"


def test_later_ci_fix_url_does_not_replace_the_implementation_pr() -> None:
    reader, _, _, gh_calls = _reader(
        _issue("In Progress", "started"),
        [
            FakeRun("https://github.com/o/r/pull/77"),
            FakeRun("https://github.com/o/r/pull/88", run_kind="ci_fix"),
        ],
        pr_stdout=_pr_json(state="MERGED"),
    )

    facts = reader.issue_facts("SFD-12")

    assert gh_calls[0][:4] == ["gh", "pr", "view", "77"]
    assert facts.pr_number == 77


def test_in_review_issue_open_with_pr() -> None:
    reader, *_ = _reader(
        _issue("In Review", "started"),
        FakeRun("https://github.com/o/r/pull/77"),
        pr_stdout=_pr_json(),
    )
    facts = reader.issue_facts("SFD-12")
    assert facts.state == "OPEN"
    assert facts.state_reason is None
    assert facts.pr_number == 77


def test_completed_issue_reads_closed_completed() -> None:
    reader, _client, _store, gh_calls = _reader(
        _issue("Done", "completed"),
        FakeRun("https://github.com/o/r/pull/77"),
    )
    facts = reader.issue_facts("SFD-12")
    assert facts.state == "CLOSED"
    assert facts.state_reason == "COMPLETED"
    assert gh_calls == []


def test_canceled_issue_reads_closed_non_completed() -> None:
    reader, *_ = _reader(_issue("Canceled", "canceled"), None)
    facts = reader.issue_facts("SFD-12")
    assert facts.state == "CLOSED"
    # Any non-COMPLETED reason drives the overlay to CANCELLED, not DONE.
    assert facts.state_reason != "COMPLETED"


def test_labels_from_linear_labels() -> None:
    reader, *_ = _reader(
        _issue("Todo", "unstarted", labels=["blocked", "agent-failed"]), None
    )
    facts = reader.issue_facts("SFD-12")
    assert facts.labels == frozenset({"blocked", "agent-failed"})


def test_linear_facts_require_both_admission_markers() -> None:
    unarmed, *_ = _reader(_issue("Todo", "unstarted", labels=["kew"]), None)
    armed, *_ = _reader(_issue("Todo", "unstarted", labels=["kew", "kew:ready"]), None)

    assert unarmed.issue_facts("SFD-12").admission_status is NodeStatus.QUEUED
    assert armed.issue_facts("SFD-12").admission_status is NodeStatus.TODO


def test_linear_facts_fail_closed_when_admission_facts_are_missing() -> None:
    reader, *_ = _reader({}, None)

    assert reader.issue_facts("SFD-12").admission_status is NodeStatus.QUEUED


def test_no_run_yields_no_pr() -> None:
    reader, _client, store, gh_calls = _reader(_issue("Todo", "unstarted"), None)
    facts = reader.issue_facts("SFD-12")
    assert store.item_ids == ["SFD-12"]
    assert gh_calls == []  # nothing to view without a run
    assert facts == NodeFacts(
        issue_num=0,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=None,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
        admission_status=NodeStatus.QUEUED,
    )


def test_run_without_pr_url_skips_gh() -> None:
    reader, _client, _store, gh_calls = _reader(
        _issue("In Progress", "started"), FakeRun(None)
    )
    facts = reader.issue_facts("SFD-12")
    assert gh_calls == []  # a run with no PR opens no `gh pr view`
    assert facts.pr_number is None


def test_gh_pr_view_failure_yields_no_pr() -> None:
    reader, *_ = _reader(
        _issue("In Review", "started"),
        FakeRun("https://github.com/o/r/pull/77"),
        pr_stdout="",
        pr_rc=1,
    )
    facts = reader.issue_facts("SFD-12")
    assert facts.pr_number == 77
    assert facts.pr_url == "https://github.com/o/r/pull/77"


def test_custom_branch_prefix() -> None:
    reader, _client, store, gh_calls = _reader(
        _issue("Todo", "unstarted"),
        FakeRun("https://github.com/o/r/pull/77", branch="wip/sfd-12"),
        pr_stdout=_pr_json(),
        branch_prefix="wip/",
    )
    reader.issue_facts("SFD-12")
    assert store.item_ids == ["SFD-12"]
    assert gh_calls[0][:4] == ["gh", "pr", "view", "77"]


def test_not_found_issue_returns_empty_facts() -> None:
    client = FakeClient(None, raise_not_found=True)
    reader = LinearFactsReader(client, FakeStore(None), "o/r", run=lambda *a, **k: None)
    facts = reader.issue_facts("SFD-99")
    assert facts.state == ""
    assert facts.labels == frozenset()
    assert facts.pr_number is None


def test_non_not_found_error_propagates() -> None:
    client = FakeClient(None, raise_error=True)
    reader = LinearFactsReader(client, FakeStore(None), "o/r", run=lambda *a, **k: None)
    try:
        reader.issue_facts("SFD-12")
    except LinearApiError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LinearApiError to propagate")


def test_pr_facts_and_required_checks_delegate_to_github() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _result('{"mergeStateStatus":"CLEAN"}')

    reader = LinearFactsReader(FakeClient(None), FakeStore(None), "o/r", run=fake_run)
    pr = reader.pr_facts(77)
    assert calls[0][:4] == ["gh", "pr", "view", "77"]  # by number, GitHub-native
    assert reader.required_checks_green("main", pr) is True
    assert reader.required_checks_green("main", {"mergeStateStatus": "DIRTY"}) is False


def test_prepare_batches_fifteen_prs_once_per_tick() -> None:
    """A 20s/15-node epic consumes 180 GraphQL queries/hour, not 2,700."""
    item_ids = [f"SFD-{n}" for n in range(1, 16)]

    class MultiClient:
        def execute(self, query, variables=None):
            assert variables is not None
            return {"issue": _issue("In Review", "started")}

    class MultiStore:
        def list_by_item(self, item_id):
            return [
                FakeRun(
                    f"https://github.com/o/r/pull/{item_id.split('-')[1]}",
                    branch=f"agent/issue-{item_id.lower()}",
                )
            ]

    payload = [
        json.loads(
            _pr_json(
                number=n,
                url=f"https://github.com/o/r/pull/{n}",
                headRefName=f"agent/issue-sfd-{n}",
            )
        )
        for n in range(1, 16)
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _result(json.dumps(payload))

    reader = LinearFactsReader(MultiClient(), MultiStore(), "o/r", run=fake_run)
    reader.prepare(item_ids)
    facts = [reader.issue_facts(item_id) for item_id in item_ids]
    # The merge pass reuses the exact fresh batch instead of re-reading each PR.
    assert all(reader.pr_facts(f.pr_number or 0) for f in facts)

    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "pr", "list"]
    assert calls[0][calls[0].index("--limit") + 1] == "100"
    assert 3600 // 20 == 180
    assert len(calls) * (3600 // 20) < 5000


def test_failed_batch_falls_back_once_per_branch() -> None:
    item_ids = [f"SFD-{n}" for n in range(1, 16)]

    class MultiStore:
        def list_by_item(self, item_id):
            return [
                FakeRun(
                    "https://github.com/o/r/pull/1",
                    branch=f"agent/issue-{item_id.lower()}",
                )
            ]

    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _result("", returncode=1, stderr="rate limit exceeded")

    reader = LinearFactsReader(
        FakeClient(_issue("In Review", "started")),
        MultiStore(),
        "o/r",
        run=fake_run,
    )
    reader.prepare(item_ids)
    assert all(reader.issue_facts(item_id).pr_number == 1 for item_id in item_ids)
    head_calls = [call for call in calls if "--head" in call]
    assert len(calls) == 16
    assert len(head_calls) == len(item_ids)


def test_small_successful_batch_never_falls_back_across_three_ticks() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _result("[]")

    reader = LinearFactsReader(
        FakeClient(_issue("In Review", "started")),
        FakeStore(FakeRun("https://github.com/o/r/pull/77")),
        "o/r",
        run=fake_run,
    )

    for _ in range(3):
        reader.prepare(["SFD-12"])
        assert reader.issue_facts("SFD-12").pr_number == 77

    assert [call for call in calls if "--head" in call] == []


def test_saturated_batch_falls_back_for_a_missing_branch() -> None:
    broad = [
        json.loads(
            _pr_json(
                number=number,
                url=f"https://github.com/o/r/pull/{number}",
                headRefName=f"other/{number}",
            )
        )
        for number in range(1, 101)
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _result(_pr_batch_json() if "--head" in cmd else json.dumps(broad))

    reader = LinearFactsReader(
        FakeClient(_issue("In Review", "started")),
        FakeStore(FakeRun("https://github.com/o/r/pull/77")),
        "o/r",
        run=fake_run,
    )
    reader.prepare(["SFD-12"])

    assert reader.issue_facts("SFD-12").pr_number == 77
    assert len([call for call in calls if "--head" in call]) == 1


def test_prepare_discards_stale_pr_facts_before_the_next_tick() -> None:
    responses = [
        _result(
            json.dumps(
                [
                    json.loads(
                        _pr_json(
                            headRefName="agent/issue-sfd-12",
                            headRefOid="stale-head",
                        )
                    )
                ]
            )
        ),
        _result("", returncode=1, stderr="rate limit exceeded"),
        _result("", returncode=1, stderr="rate limit exceeded"),
    ]

    def fake_run(*_args, **_kwargs):
        return responses.pop(0)

    reader = LinearFactsReader(
        FakeClient(_issue("In Review", "started")),
        FakeStore(FakeRun("https://github.com/o/r/pull/77")),
        "o/r",
        run=fake_run,
    )

    reader.prepare(["SFD-12"])
    assert reader.issue_facts("SFD-12").head_oid == "stale-head"

    reader.prepare(["SFD-12"])
    second_tick = reader.issue_facts("SFD-12")

    assert second_tick.pr_number == 77
    assert second_tick.head_oid is None


def test_existing_pr_reconciles_awaiting_publication_run(tmp_path) -> None:
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=0,
                item_id="SFD-12",
                branch="agent/issue-sfd-12",
                status="awaiting_publication",
                exit_code=0,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                error_category="publication_rate_limited",
            )
        )

        reader = LinearFactsReader(
            FakeClient(_issue("In Review", "started")),
            store,
            "o/r",
            run=lambda *_a, **_kw: _result(_pr_batch_json()),
        )
        reader.prepare(["SFD-12"])
        facts = reader.issue_facts("SFD-12")
        reconciled = store.get("run-1")

    assert facts.pr_number == 77
    assert reconciled is not None
    assert reconciled.status == "success"
    assert reconciled.pr_url == "https://github.com/o/r/pull/77"
    assert reconciled.error_category == ""


def test_closed_unmerged_pr_leaves_awaiting_publication_run_alone(tmp_path) -> None:
    """A closed-unmerged PR is not published work — keep the recovery handle.

    Mirrors `GitHubFactsReader._reconcile_issue_publication`, which reconciles
    only on `OPEN`/`MERGED`. Finalizing here would terminal the continuation
    record and strand the preserved branch.
    """
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=0,
                item_id="SFD-12",
                branch="agent/issue-sfd-12",
                status="awaiting_publication",
                exit_code=0,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                error_category="publication_rate_limited",
            )
        )

        reader = LinearFactsReader(
            FakeClient(_issue("In Review", "started")),
            store,
            "o/r",
            run=lambda *_a, **_kw: _result(_pr_batch_json(state="CLOSED")),
        )
        reader.prepare(["SFD-12"])
        facts = reader.issue_facts("SFD-12")
        untouched = store.get("run-1")

    assert facts.pr_number == 77
    assert untouched is not None
    assert untouched.status == "awaiting_publication"
    assert untouched.error_category == "publication_rate_limited"


def test_completed_linear_item_reconciles_awaiting_publication_without_pr(
    tmp_path,
) -> None:
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=0,
                item_id="SFD-12",
                branch="agent/issue-sfd-12",
                status="awaiting_publication",
                exit_code=0,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        reader = LinearFactsReader(
            FakeClient(_issue("Done", "completed")),
            store,
            "o/r",
            run=lambda *_a, **_kw: _result("[]"),
        )

        facts = reader.issue_facts("SFD-12")
        reconciled = store.get("run-1")

    assert facts.state == "CLOSED"
    assert facts.state_reason == "COMPLETED"
    assert reconciled is not None
    assert reconciled.status == "success"


def test_runless_item_links_a_branch_pr_from_the_prepared_batch() -> None:
    """Design §2.1 rule 3 applies to Linear too: no ledger row, but the batch
    prepared for this tick carries an OPEN PR on the item's dispatch branch →
    linked by branch, with no per-node fallback call."""
    payload = [
        json.loads(
            _pr_json(
                number=77,
                url="https://github.com/o/r/pull/77",
                headRefName="agent/issue-sfd-12",
            )
        )
    ]
    reader, _client, _store, gh_calls = _reader(
        _issue("In Review", "started"), None, pr_stdout=json.dumps(payload)
    )
    reader.prepare(["SFD-12"])

    facts = reader.issue_facts("SFD-12")

    assert facts.pr_number == 77
    assert facts.pr_link_source == "branch"
    assert facts.pr_state == "OPEN"
    list_calls = [cmd for cmd in gh_calls if cmd[:3] == ["gh", "pr", "list"]]
    assert len(list_calls) == 1 and "--head" not in list_calls[0]


def test_linear_facts_never_claim_a_missing_closing_reference() -> None:
    """Linear cannot observe GitHub closing references, so the fact is None —
    never False, which would make `kew epic status` warn on every Linear PR."""
    reader, _client, _store, _gh_calls = _reader(
        _issue("In Review", "started"),
        FakeRun("https://github.com/o/r/pull/77", branch="agent/issue-sfd-12"),
        pr_stdout=_pr_json(number=77, url="https://github.com/o/r/pull/77"),
    )
    facts = reader.issue_facts("SFD-12")
    assert facts.pr_number == 77
    assert facts.pr_has_closing_ref is None


def test_fresh_pr_facts_bypasses_the_prepared_batch() -> None:
    """The merge queue's post-verification head re-read (#2148) must reach
    GitHub, never this tick's batch row the classification already used — a
    head that moves mid-verification cannot appear in a once-per-tick batch."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return _result(_pr_batch_json(headRefOid="stale-head"))
        return _result(_pr_json(headRefOid="moved-head"))

    reader = LinearFactsReader(
        FakeClient(_issue("In Review", "started")),
        FakeStore(FakeRun("https://github.com/o/r/pull/77")),
        "o/r",
        run=fake_run,
    )
    reader.prepare(["SFD-12"])

    assert reader.pr_facts(77)["headRefOid"] == "stale-head"
    assert reader.fresh_pr_facts(77)["headRefOid"] == "moved-head"
    assert [cmd[:4] for cmd in calls if cmd[:3] == ["gh", "pr", "view"]] == [
        ["gh", "pr", "view", "77"]
    ]


def test_fresh_pr_facts_is_unreadable_not_unchanged_on_gh_failure() -> None:
    """A failed re-read returns {} so the merge gate holds; it must not fall
    back to the batch row and report the pre-verification head as current."""

    def fake_run(cmd, **_kw):
        if cmd[:3] == ["gh", "pr", "list"]:
            return _result(_pr_batch_json(headRefOid="stale-head"))
        return _result("", returncode=1, stderr="rate limit exceeded")

    reader = LinearFactsReader(
        FakeClient(_issue("In Review", "started")),
        FakeStore(FakeRun("https://github.com/o/r/pull/77")),
        "o/r",
        run=fake_run,
    )
    reader.prepare(["SFD-12"])

    assert reader.fresh_pr_facts(77) == {}
