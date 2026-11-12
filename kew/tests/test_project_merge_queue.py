"""The project's merge queue, verified with ``kew verify`` (#2814).

Git is real — a bare ``origin`` carrying ``main`` and one ``refs/pull/<n>/head``
per PR, and a clone the queue works in — so the fetch, the worktree and the
squash of the queued heads onto main are the code's own. The forge (``gh api``)
is faked at the process boundary, and its merge squashes the head onto
origin's main with real git, as GitHub does. ``kew verify`` is faked by a
verifier that reads the candidate tree it is handed: a tree carrying a PR's
``broken.py`` fails, as its tests would.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.unchecked_reviews import record_reviewed

from kew import subprocess_utils
from kew.audit import Actor, emit
from kew.config import DispatchConfig, MergeQueueConfig
from kew.drive_merge import REPORT_QUEUED_EVENT
from kew.epic_holds import (
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    merge_gate_ack_key,
    merge_gate_hold_reason,
)
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_BROWSER_UNCHECKED,
    merge_queue_lock,
    merge_queue_lock_path,
)
from kew.merge_verify import (
    MUTATION_EVENT,
    VERIFY_EVENT,
    MutationReport,
    VerifyResult,
)
from kew.notifier import NotificationEvent
from kew.project_merge_queue import (
    SETTLED_EVENT,
    GhForge,
    PullState,
    QueueError,
    QueueRunner,
    combined_body,
    dequeue,
    enqueue,
)
from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore
from kew.service.needs_you import needs_you_set

REPO = "o/r"
OPERATOR = Actor(type="human", id="tom")


@pytest.fixture(autouse=True)
def _tools() -> None:
    subprocess_utils._register_optional_tool("git")


def _git(cwd: Path, *args: str) -> str:
    # A fixed identity: the verify host has no global git config.
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.test"]
    return subprocess.run(
        ["git", "-C", str(cwd), *identity, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", path)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path)
    return _git(repo, "rev-parse", "HEAD")


class _Origin:
    """A bare origin with main, PR heads at refs/pull/<n>/head, and a clone."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.work = root / "author"
        self.clone = root / "clone"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True
        )
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        _commit(self.work, "README.md", "hello\n")
        _git(self.work, "push", "-q", str(self.origin), "main")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.clone)], check=True
        )
        self.heads: dict[int, str] = {}
        self.moves = 0

    def open_pr(self, pr: int, path: str) -> str:
        _git(self.work, "checkout", "-q", "-b", f"pr-{pr}", "main")
        head = _commit(self.work, path, f"PR {pr}\n")
        _git(self.work, "push", "-q", str(self.origin), f"pr-{pr}:refs/pull/{pr}/head")
        _git(self.work, "checkout", "-q", "main")
        self.heads[pr] = head
        return head

    def open_changed_pr(self, pr: int, path: str, before: str, after: str) -> str:
        """Push a PR that changes an existing file on main."""
        _commit(self.work, path, before)
        _git(self.work, "push", "-q", str(self.origin), "main")
        _git(self.work, "checkout", "-q", "-b", f"pr-{pr}", "main")
        head = _commit(self.work, path, after)
        _git(self.work, "push", "-q", str(self.origin), f"pr-{pr}:refs/pull/{pr}/head")
        _git(self.work, "checkout", "-q", "main")
        self.heads[pr] = head
        return head

    def push_pr(self, pr: int, path: str) -> str:
        _git(self.work, "checkout", "-q", f"pr-{pr}")
        head = _commit(self.work, path, "pushed while queued\n")
        _git(self.work, "push", "-q", str(self.origin), f"pr-{pr}:refs/pull/{pr}/head")
        _git(self.work, "checkout", "-q", "main")
        self.heads[pr] = head
        return head

    def _sync_main(self) -> None:
        _git(self.work, "checkout", "-q", "main")
        _git(self.work, "fetch", "-q", str(self.origin), "main")
        _git(self.work, "reset", "-q", "--hard", "FETCH_HEAD")

    def move_main(self, text: str) -> str:
        self._sync_main()
        self.moves += 1
        sha = _commit(self.work, f"notes/{self.moves}.md", text)
        _git(self.work, "push", "-q", str(self.origin), "main")
        return sha

    def squash(self, pr: int, extra: str | None = None) -> str:
        """Squash PR *pr*'s head onto origin's main, as GitHub's merge does."""
        self._sync_main()
        _git(self.work, "merge", "-q", "--squash", self.heads[pr])
        if extra is not None:
            (self.work / extra).write_text("not what was verified\n")
            _git(self.work, "add", extra)
        _git(
            self.work,
            *("-c", "user.name=gh", "-c", "user.email=gh@gh", "commit"),
            *("-q", "--allow-empty", "-m", f"PR {pr} (#{pr})"),
        )
        _git(self.work, "push", "-q", str(self.origin), "main")
        return _git(self.work, "rev-parse", "HEAD")

    def main(self) -> str:
        return _git(self.origin, "rev-parse", "main")


class _Gh:
    """Real git; a fake ``gh api`` serving PRs, squash-merging onto origin's
    main as GitHub does, and recording merges and comments."""

    def __init__(self, origin: _Origin, *, refuse_merge: int | None = None) -> None:
        self.origin = origin
        self.merged: list[tuple[int, str]] = []
        self.landed: dict[int, str] = {}
        self.comments: list[tuple[int, str]] = []
        #: ``gh pr comment`` posts: the post-merge mutation reports.
        self.pr_comments: list[tuple[int, str]] = []
        self.refuse_merge = refuse_merge
        #: Per PR: the base branch it targets, when not main.
        self.targets: dict[int, str] = {}
        self.drafts: set[int] = set()
        #: Per PR: a merge answer to give instead of merging.
        self.answers: dict[int, str] = {}
        #: Per PR: a file GitHub's squash adds that no verify saw.
        self.extra: dict[int, str] = {}
        #: Per PR: the live body GitHub returns instead of the default.
        self.bodies: dict[int, str] = {}
        #: Called with the PR number after each merge lands.
        self.after_merge: Any = None

    def _merge(self, cmd: list[str], pr: int, fields: dict[str, str]) -> Any:
        if pr == self.refuse_merge:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 405 not mergeable")
        if pr in self.answers:
            return subprocess.CompletedProcess(cmd, 0, self.answers[pr], "")
        assert fields["merge_method"] == "squash"
        assert fields["sha"] == self.origin.heads[pr]
        landed = self.origin.squash(pr, self.extra.get(pr))
        self.merged.append((pr, fields["sha"]))
        self.landed[pr] = landed
        if self.after_merge is not None:
            self.after_merge(pr)
        answer = {"sha": landed, "merged": True, "message": "Pull Request merged"}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(answer), "")

    def pull(self, pr: int) -> dict[str, Any]:
        return {
            "state": "open",
            "merged": pr in self.landed,
            "merge_commit_sha": self.landed.get(pr),
            "draft": pr in self.drafts,
            "head": {"sha": self.origin.heads[pr]},
            "base": {"ref": self.targets.get(pr, "main"), "repo": {"full_name": REPO}},
            "title": f"PR {pr}",
            "body": self.bodies.get(pr, f"Body of {pr}."),
        }

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "git":
            return subprocess.run(cmd, capture_output=True, text=True, check=False)
        if cmd[:3] == ["gh", "pr", "comment"]:
            self.pr_comments.append((int(cmd[3]), cmd[cmd.index("--body") + 1]))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        assert cmd[:2] == ["gh", "api"], cmd
        path = cmd[-1]
        fields = dict(a.split("=", 1) for a in cmd[2:-1] if "=" in a)
        if path.endswith("/merge"):
            return self._merge(cmd, int(path.split("/")[-2]), fields)  # type: ignore[no-any-return]
        if path.endswith("/comments"):
            if "body" not in fields:
                comments = [{"body": body} for _pr, body in self.comments]
                payload: Any = [comments] if "--slurp" in cmd else comments
                return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
            self.comments.append((int(path.split("/")[-2]), fields["body"]))
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        data = self.pull(int(path.split("/")[-1]))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")


class _Verify:
    """``kew verify`` reading the candidate: a ``broken.py`` in it fails."""

    def __init__(self, clone: Path, during: Any = None) -> None:
        self.clone = clone
        self.trees: list[list[str]] = []
        self.bodies: list[str | None] = []
        self.bases: list[str] = []
        #: Per verify: the acceptance contracts it was handed, as (path, version).
        self.contracts: list[list[tuple[str, int]]] = []
        #: Per verify: whether it was told to run the full suite.
        self.full: list[bool] = []
        self.during = during

    def __call__(
        self,
        config: Any,
        *,
        store: Any,
        ref: str,
        repo_root: Path,
        pr_body: Any,
        base_sha: str,
        contracts: Any = (),
        require_receipt: bool = False,
        force_full: bool = False,
    ) -> Any:
        files = _git(self.clone, "ls-tree", "-r", "--name-only", ref).split()
        self.trees.append(sorted(f for f in files if f.startswith("pr/")))
        self.full.append(force_full)
        self.bodies.append(pr_body)
        self.bases.append(base_sha)
        self.contracts.append([(c.path, c.version) for c in contracts])
        if self.during is not None:
            self.during()
        code = 1 if any(f.endswith("broken.py") for f in files) else 0
        receipt = 1 if require_receipt else None
        return None, VerifyResult(
            host="gcp", exit_code=code, seconds=1.0, log="", receipt=receipt
        )


class _Report:
    """The post-merge mutation report job: records what it was asked to run,
    and records :data:`MUTATION_EVENT` as :func:`run_mutation_report` does."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, config: Any, **kw: Any) -> MutationReport:
        self.calls.append(kw)
        emit(
            kw["store"],
            MUTATION_EVENT,
            actor=Actor(type="policy", id="test"),
            payload={"pr": kw["pr_number"], "ref_sha": kw["merged_sha"]},
        )
        return MutationReport(outcome="completed", selected=2, killed=2)


def _config(tmp_path: Path, train: int = 1) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.merge_queue = MergeQueueConfig(train=train)
    return config


def _runner(
    store: Any,
    tmp_path: Path,
    origin: _Origin,
    gh: _Gh,
    verify: _Verify,
    train: int = 1,
    notices: list[NotificationEvent] | None = None,
) -> QueueRunner:
    return QueueRunner(
        _config(tmp_path, train),
        store,
        repo=REPO,
        repo_root=origin.clone,
        run=gh,
        verify=verify,
        probe=lambda _config: None,
        notify=(notices if notices is not None else []).append,
    )


def _ready(store: Any, head: str, verdict: str = "READY") -> None:
    """kew's review recorded *verdict* for *head*, as the review stage does."""
    store.record_review_round(
        "epic", f"node-{head[:8]}", head_sha=head, verdict=verdict
    )


def _add(store: Any, gh: _Gh, pr: int, session: str = "s1") -> None:
    _ready(store, gh.origin.heads[pr])
    enqueue(
        store,
        GhForge(REPO, gh),
        pr=pr,
        head=gh.origin.heads[pr],
        session=session,
        actor=OPERATOR,
    )


def _add_with_sidecar_missing_test_changes(
    store: Any, gh: _Gh, pr: int, tmp_path: Path
) -> None:
    head = gh.origin.heads[pr]
    sidecar = tmp_path / f"review-sidecar-{head}.json"
    sidecar.write_text(json.dumps({"verdict": "READY"}))
    store.record_review_round(
        "epic",
        f"node-{head[:8]}",
        head_sha=head,
        verdict="READY",
        sidecar_path=str(sidecar),
        sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    )
    enqueue(
        store,
        GhForge(REPO, gh),
        pr=pr,
        head=head,
        session="s1",
        actor=OPERATOR,
    )


def _add_with_unchecked(
    store: Any,
    gh: _Gh,
    pr: int,
    tmp_path: Path,
    kinds: list[str],
    *,
    accepted: bool = False,
) -> str:
    """Queue *pr*, reviewed READY — or an accepted NEEDS_CHANGES — with *kinds*
    in ``not_checked`` (#3421)."""
    head = gh.origin.heads[pr]
    record_reviewed(
        store,
        tmp_path,
        epic_ref="epic",
        item_id=f"node-{head[:8]}",
        head=head,
        kinds=kinds,
        accepted=accepted,
        repo=REPO,
    )
    enqueue(store, GhForge(REPO, gh), pr=pr, head=head, session="s1", actor=OPERATOR)
    return f"node-{head[:8]}"


def _states(store: Any) -> dict[int, tuple[str, str | None]]:
    return {
        e.pr: (e.state, e.reason)
        for e in store.list_merge_queue(REPO, include_settled=True)
    }


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


@pytest.fixture
def origin(tmp_path: Path) -> _Origin:
    return _Origin(tmp_path)


def test_two_sessions_enqueuing_alternately_merge_in_fifo_order_one_at_a_time(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12, 13, 14):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    _add(store, gh, 11, "session-a")
    _add(store, gh, 12, "session-b")
    _add(store, gh, 13, "session-a")
    _add(store, gh, 14, "session-b")
    blocked: list[str | None] = []
    other = _runner(store, tmp_path, origin, gh, _Verify(origin.clone))
    # While one session's verify runs, the other's pass cannot take an entry.
    verify = _Verify(origin.clone, during=lambda: blocked.append(other.run_once().held))
    runner = _runner(store, tmp_path, origin, gh, verify)

    passes = [[e.pr for e in runner.run_once().merged] for _ in range(4)]

    assert passes == [[11], [12], [13], [14]]
    assert gh.merged == [(pr, origin.heads[pr]) for pr in (11, 12, 13, 14)]
    # Each verify is built on the main the merges before it landed.
    assert verify.trees == [
        ["pr/11.py"],
        ["pr/11.py", "pr/12.py"],
        ["pr/11.py", "pr/12.py", "pr/13.py"],
        ["pr/11.py", "pr/12.py", "pr/13.py", "pr/14.py"],
    ]
    assert len(blocked) == 4 and all(blocked)
    assert store.list_merge_queue(REPO) == []


def test_a_candidate_whose_verify_fails_is_not_merged_and_says_why(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/broken.py")
    gh = _Gh(origin)
    _add(store, gh, 11)

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert gh.merged == [] and done.merged == []
    state, reason = _states(store)[11]
    assert state == "failed"
    assert reason is not None and "kew verify failed on gcp" in reason
    assert "exit 1" in reason and "#11" in reason
    assert len(gh.comments) == 1 and gh.comments[0][0] == 11
    assert gh.comments[0][1].startswith(f"kew merge queue: failed — {reason}.\n\n")
    assert "<!-- kew-merge-queue-verdict:" in gh.comments[0][1]
    settled = store.list_audit_events(limit=None, event=SETTLED_EVENT)
    assert [e.payload["state"] for e in settled] == ["failed"]


def test_a_failed_verdict_comment_is_delivered_once_after_restart(
    tmp_path: Path, origin: _Origin
) -> None:
    """A 503 after GitHub accepts the comment cannot duplicate it on retry."""

    class FailFirstVerdictPost(_Gh):
        failures = 1

        def __call__(
            self, cmd: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            is_verdict_post = (
                cmd[:2] == ["gh", "api"]
                and cmd[-1].endswith("/comments")
                and any(arg.startswith("body=") for arg in cmd)
            )
            if is_verdict_post and self.failures:
                self.failures -= 1
                super().__call__(cmd, **kwargs)
                return subprocess.CompletedProcess(cmd, 1, "", "HTTP 503")
            return super().__call__(cmd, **kwargs)

    origin.open_pr(11, "pr/broken.py")
    gh = FailFirstVerdictPost(origin)
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as before:
        _add(before, gh, 11)

        _runner(before, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

        [failed] = before.list_merge_queue(REPO, include_settled=True)
        assert failed.state == "failed" and failed.reported_at is None
        assert len(gh.comments) == 1

    with SqliteRunStore(db) as after:
        restarted = _runner(after, tmp_path, origin, gh, _Verify(origin.clone))

        restarted.run_once()
        restarted.run_once()

        [reported] = after.list_merge_queue(REPO, include_settled=True)
        assert reported.reported_at is not None
        assert len(gh.comments) == 1
        assert gh.comments[0][0] == 11
        assert "kew merge queue: failed" in gh.comments[0][1]


def test_main_moving_during_a_verify_refuses_and_requeues_the_entry_once(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    moving = _Verify(origin.clone, during=lambda: origin.move_main("moved\n"))

    first = _runner(store, tmp_path, origin, gh, moving).run_once()

    assert gh.merged == []
    assert [e.pr for e in first.requeued] == [11]
    [entry] = store.list_merge_queue(REPO)
    assert (entry.pr, entry.state, entry.requeues) == (11, "queued", 1)
    assert entry.reason is not None and "moved from" in entry.reason

    # Moving again under the same entry drops it rather than looping forever.
    again = _runner(store, tmp_path, origin, gh, moving).run_once()
    assert again.requeued == [] and gh.merged == []
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "again" in reason


def test_a_requeued_entry_merges_once_main_holds_still(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    moving = _Verify(origin.clone, during=lambda: origin.move_main("moved\n"))
    _runner(store, tmp_path, origin, gh, moving).run_once()

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert [e.pr for e in done.merged] == [11]
    assert gh.merged == [(11, origin.heads[11])]


def test_queue_entries_survive_a_drive_restart(tmp_path: Path, origin: _Origin) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as before:
        _add(before, gh, 11, "session-a")
        _add(before, gh, 12, "session-b")

    with SqliteRunStore(db) as after:
        queued = [(e.pr, e.session) for e in after.list_merge_queue(REPO)]
        runner = _runner(after, tmp_path, origin, gh, _Verify(origin.clone))
        merged = [[e.pr for e in runner.run_once().merged] for _ in range(2)]

    assert queued == [(11, "session-a"), (12, "session-b")]
    assert merged == [[11], [12]]


def test_a_head_that_changes_while_queued_is_dropped_with_a_reason(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    queued = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    pushed = origin.push_pr(11, "pr/11b.py")
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [] and verify.trees == []
    state, reason = _states(store)[11]
    assert state == "dropped"
    assert reason is not None and queued[:12] in reason and pushed[:12] in reason


def test_a_gate_edit_without_a_head_receipt_is_held_before_verify(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    head = origin.open_pr(11, "kew/scripts/verify.sh")
    gh = _Gh(origin)
    _add(store, gh, 11)
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify, notices=notices).run_once()

    assert done.held == (f"merge_gate_touched@{head}: kew/scripts/verify.sh")
    assert verify.bases == [] and gh.merged == []
    [entry] = store.list_merge_queue(REPO)
    assert entry.state == "queued" and entry.reason is None
    [hold] = store.list_epic_holds("epic")
    assert hold.item_id == f"node-{head[:8]}" and hold.reason == done.held
    assert len(notices) == 1 and notices[0].pr_number == 11


def test_a_gate_edit_with_a_receipt_for_its_head_is_verified(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    head = origin.open_pr(11, "kew/scripts/verify.sh")
    gh = _Gh(origin)
    _add(store, gh, 11)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)
    first = runner.run_once()
    item_id = f"node-{head[:8]}"
    assert first.held is not None
    released = store.remove_epic_hold(
        "epic",
        item_id,
        receipt_key=merge_gate_ack_key("epic", item_id),
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )
    assert released is not None

    second = runner.run_once()

    assert [entry.pr for entry in second.merged] == [11]
    assert len(verify.bases) == 1
    assert gh.merged == [(11, head)]


@pytest.mark.parametrize(
    ("before", "after", "flag"),
    [
        pytest.param(
            "def test_widget():\n    assert 1 == 1\n    assert True\n",
            "def test_widget():\n    assert True\n",
            "asserts_removed",
            id="removed-assertion",
        ),
        pytest.param(
            "def test_widget():\n    value = calculate()\n    assert value == 1\n",
            "def test_widget():\n    value = calculate()\n    assert value == 2\n",
            "changed_expected",
            id="changed-expected-value",
        ),
        pytest.param(
            "def test_widget():\n    assert True\n",
            '@pytest.mark.skip(reason="later")\ndef test_widget():\n    assert True\n',
            "skip_added",
            id="added-skip",
        ),
    ],
)
def test_test_integrity_flags_unanswered_by_ready_review_hold_before_verify(
    store: Any,
    tmp_path: Path,
    origin: _Origin,
    before: str,
    after: str,
    flag: str,
) -> None:
    head = origin.open_changed_pr(11, "tests/test_widget.py", before, after)
    gh = _Gh(origin)
    _add_with_sidecar_missing_test_changes(store, gh, 11, tmp_path)
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, notices=notices)

    first = runner.run_once()

    assert first.held == (
        f"merge_gate_touched@{head}: unanswered test-integrity flags {flag}"
    )
    assert verify.bases == [] and gh.merged == []
    [entry] = store.list_merge_queue(REPO)
    assert entry.state == "queued" and entry.reason is None
    [hold] = store.list_epic_holds("epic")
    assert hold.item_id == f"node-{head[:8]}" and hold.reason == first.held
    assert len(notices) == 1 and notices[0].pr_number == 11

    released = store.remove_epic_hold(
        "epic",
        hold.item_id,
        receipt_key=merge_gate_ack_key("epic", hold.item_id),
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )
    assert released is not None

    second = runner.run_once()

    assert [entry.pr for entry in second.merged] == [11]
    assert len(verify.bases) == 1


def test_a_train_of_three_passing_prs_merges_all_after_one_verify_in_order(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12, 13):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12, 13):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify, train=3).run_once()

    assert done.verifies == 1
    assert verify.trees == [["pr/11.py", "pr/12.py", "pr/13.py"]]
    assert gh.merged == [(pr, origin.heads[pr]) for pr in (11, 12, 13)]
    assert [s for s, _ in _states(store).values()] == ["merged"] * 3


def test_a_train_verify_receives_no_live_pr_body(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify, train=2).run_once()

    assert verify.bodies == [None]


def test_a_body_change_after_enqueue_cannot_add_a_behavior_exception(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/broken.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    gh.bodies[11] = (
        "**Behavior changes**\n"
        "- tests/test_old.py::test_contract — the new behavior replaces it"
    )
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify, notices=notices).run_once()

    assert gh.merged == [] and verify.trees == []
    state, reason = _states(store)[11]
    assert state == "dropped"
    assert reason is not None and "body changed after enqueue" in reason
    assert "re-enqueue after review" in reason
    assert len(notices) == 1 and notices[0].pr_number == 11


def test_a_declaring_pr_is_verified_alone_and_cannot_excuse_the_next_pr(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/broken.py")
    gh = _Gh(origin)
    gh.bodies[11] = (
        "**Behavior changes**\n"
        "- tests/test_old.py::test_contract — the new behavior replaces it"
    )
    for pr in (11, 12):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, train=2)

    first = runner.run_once()
    second = runner.run_once()

    assert [entry.pr for entry in first.merged] == [11]
    assert second.merged == [] and _states(store)[12][0] == "failed"
    assert verify.trees == [["pr/11.py"], ["pr/11.py", "pr/broken.py"]]
    assert verify.bodies[0] is not None
    assert "tests/test_old.py::test_contract" in verify.bodies[0]
    assert verify.bodies[1] is None


def test_combined_body_grants_only_one_entries_persisted_declarations() -> None:
    from kew.merge_verify import declared_behavior_changes
    from kew.run_store_merge_queue import MergeQueueEntry

    now = datetime.now(UTC)

    def entry(pr: int, declarations: tuple[str, ...]) -> MergeQueueEntry:
        return MergeQueueEntry(
            pr,
            REPO,
            pr,
            "a" * 40,
            "s",
            "queued",
            None,
            0,
            now,
            now,
            None,
            declarations,
            None,
            None,
            None,
        )

    declared = ("tests/test_a.py::test_x",)
    body = combined_body([entry(1, declared)])

    assert declared_behavior_changes(body) == {"tests/test_a.py::test_x"}
    assert combined_body([entry(1, declared), entry(2, ())]) is None


def test_a_train_with_one_failing_pr_bisects_and_merges_the_other_two(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/broken.py")
    origin.open_pr(13, "pr/13.py")
    gh = _Gh(origin)
    for pr in (11, 12, 13):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify, train=3).run_once()

    assert gh.merged == [(11, origin.heads[11]), (13, origin.heads[13])]
    states = _states(store)
    assert states[11][0] == states[13][0] == "merged"
    state, reason = states[12]
    assert state == "failed"
    assert reason is not None and "kew verify failed" in reason and "#12" in reason
    # The whole train, then its prefixes, then the train without the culprit.
    assert verify.trees == [
        ["pr/11.py", "pr/13.py", "pr/broken.py"],
        ["pr/11.py", "pr/broken.py"],
        ["pr/11.py"],
        ["pr/11.py", "pr/13.py"],
    ]
    assert done.verifies == 4


def test_train_of_one_is_a_plain_one_at_a_time_queue(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/broken.py")
    origin.open_pr(13, "pr/13.py")
    gh = _Gh(origin)
    for pr in (11, 12, 13):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, train=1)

    passes = [runner.run_once() for _ in range(3)]

    assert [p.verifies for p in passes] == [1, 1, 1]
    assert verify.trees == [
        ["pr/11.py"],
        ["pr/11.py", "pr/broken.py"],
        ["pr/11.py", "pr/13.py"],
    ]
    assert gh.merged == [(11, origin.heads[11]), (13, origin.heads[13])]
    assert _states(store)[12][0] == "failed"


def test_a_pr_that_does_not_merge_cleanly_onto_the_train_is_failed(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/same.py")
    origin.open_pr(12, "pr/same.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    # Same path, different content: #12 conflicts with #11 queued before it.
    _git(origin.work, "checkout", "-q", "pr-12")
    origin.heads[12] = _commit(origin.work, "pr/same.py", "conflicting\n")
    _git(origin.work, "push", "-q", "-f", str(origin.origin), "pr-12:refs/pull/12/head")
    _git(origin.work, "checkout", "-q", "main")
    store.settle_merge_entry(store.list_merge_queue(REPO)[1].id, "removed", "re-add")
    _add(store, gh, 12)

    _runner(store, tmp_path, origin, gh, _Verify(origin.clone), train=2).run_once()

    assert gh.merged == [(11, origin.heads[11])]
    state, reason = _states(store)[12]
    assert state == "failed" and reason is not None and "merge cleanly" in reason


def test_a_refused_merge_fails_that_entry_and_leaves_the_rest_queued(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin, refuse_merge=11)
    for pr in (11, 12):
        _add(store, gh, pr)

    _runner(store, tmp_path, origin, gh, _Verify(origin.clone), train=2).run_once()

    assert gh.merged == []
    state, reason = _states(store)[11]
    assert state == "failed" and reason is not None and "405" in reason
    assert [e.pr for e in store.list_merge_queue(REPO)] == [12]


def test_a_pass_while_another_holds_the_queue_lock_takes_nothing(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    config = _config(tmp_path)
    verify = _Verify(origin.clone)

    with merge_queue_lock(merge_queue_lock_path(config)) as lease:
        assert lease.acquired
        done = _runner(store, tmp_path, origin, gh, verify).run_once()

    assert done.held and verify.trees == [] and gh.merged == []
    assert [e.pr for e in store.list_merge_queue(REPO)] == [11]


def test_cli_queue_run_returns_two_when_another_pass_holds_the_lock(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    from typer.testing import CliRunner

    from kew.cli import app

    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{tmp_path}"\n'
    )
    lock_path = store.merge_queue_ledger_lock_path()

    assert lock_path is not None
    with merge_queue_lock(lock_path) as lease:
        assert lease.acquired
        result = CliRunner().invoke(
            app, ["--project", str(project), "merge", "queue", "run"]
        )

    assert result.exit_code == 2, result.output
    assert "nothing settled" in result.output
    assert [entry.pr for entry in store.list_merge_queue(REPO)] == [11]


def test_enqueue_refuses_a_head_that_is_not_the_pr_head(
    store: Any, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)

    _ready(store, origin.heads[11])
    with pytest.raises(QueueError, match="head is"):
        enqueue(
            store, GhForge(REPO, gh), pr=11, head="b" * 40, session="s", actor=OPERATOR
        )
    with pytest.raises(QueueError, match="40-hex"):
        enqueue(
            store, GhForge(REPO, gh), pr=11, head="abc", session="s", actor=OPERATOR
        )

    assert store.list_merge_queue(REPO, include_settled=True) == []


def test_enqueue_twice_keeps_one_entry_and_remove_takes_it_out(
    store: Any, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11, "session-a")
    _add(store, gh, 11, "session-b")

    assert [(e.pr, e.session) for e in store.list_merge_queue(REPO)] == [
        (11, "session-a")
    ]
    removed = dequeue(store, REPO, pr=11, actor=OPERATOR)
    assert removed is not None and removed.pr == 11
    assert store.list_merge_queue(REPO) == []
    assert _states(store)[11] == ("removed", "removed by tom")
    assert dequeue(store, REPO, pr=11, actor=OPERATOR) is None


def test_v71_migration_applies_on_a_v70_ledger_copy_and_is_idempotent(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live" / "dispatch.db"
    live.parent.mkdir()
    with SqliteRunStore(str(live)) as fresh:
        fresh.record_review_round("e", "1", head_sha="a" * 40, verdict="READY")
        fresh._conn.execute("DROP TABLE merge_queue")
        fresh._conn.execute("DROP TABLE merge_queue_suspects")
        fresh._conn.execute("ALTER TABLE epic_retries DROP COLUMN superseded_run_ids")
        fresh._conn.execute("ALTER TABLE epic_retries DROP COLUMN superseded_before")
        fresh._conn.execute("UPDATE schema_version SET version = 70")
        fresh._conn.commit()

    copy = tmp_path / "copy" / "dispatch.db"
    copy.parent.mkdir()
    shutil.copy2(live, copy)

    migration = object.__new__(SqliteRunStore)
    copied = sqlite3.connect(copy)
    try:
        cursor = copied.cursor()
        migration._apply_v71(cursor)
        copied.commit()
        version = copied.execute("SELECT version FROM schema_version").fetchone()
        assert version is not None and version[0] == 71
        migration._apply_v71(cursor)
        copied.commit()
    finally:
        copied.close()

    with SqliteRunStore(str(copy)) as migrated:
        entry, created = migrated.enqueue_merge(
            REPO, 11, "a" * 40, session="s", behavior_changes=()
        )
        assert created and migrated.list_merge_queue(REPO) == [entry]
        assert migrated.latest_review_round("1") is not None
        version = migrated._conn.execute("SELECT version FROM schema_version")
        assert version.fetchone()[0] == CURRENT_SCHEMA_VERSION

        cursor = migrated._conn.cursor()
        migrated._apply_v71(cursor)
        migrated._conn.commit()
        assert migrated.list_merge_queue(REPO) == [entry]

    with SqliteRunStore(str(copy)) as reopened:
        assert reopened.list_merge_queue(REPO) == [entry]
        version = reopened._conn.execute("SELECT version FROM schema_version")
        assert version.fetchone()[0] == CURRENT_SCHEMA_VERSION

    source = sqlite3.connect(live)
    try:
        version = source.execute("SELECT version FROM schema_version").fetchone()
        assert version is not None and version[0] == 70
        queue = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'merge_queue'"
        ).fetchone()
        assert queue is None
    finally:
        source.close()
    assert list(copy.parent.glob(f"dispatch.db.bak-pre-v{CURRENT_SCHEMA_VERSION}-*"))


def test_v73_migration_keeps_a_v72_stop_and_never_calls_it_red(
    tmp_path: Path,
) -> None:
    """A pre-v73 suspect cannot say whether its main was verified red or only
    landed a mismatched tree, so it survives as "not verified green"."""
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as fresh:
        fresh._conn.execute("DROP TABLE merge_queue_suspects")
        fresh._conn.execute(
            "CREATE TABLE merge_queue_suspects (repo TEXT PRIMARY KEY,"
            " reason TEXT NOT NULL, red_on TEXT, since TEXT NOT NULL)"
        )
        fresh._conn.execute(
            "INSERT INTO merge_queue_suspects VALUES (?, ?, ?, ?)",
            (REPO, "#9 landed unverified", "cd" * 20, "2026-10-15T09:00:00+00:00"),
        )
        fresh._conn.execute("UPDATE schema_version SET version = 72")
        fresh._conn.commit()

    with SqliteRunStore(str(db)) as migrated:
        (stop,) = migrated.list_merge_queue_suspects()
        version = migrated._conn.execute("SELECT version FROM schema_version")
        assert version.fetchone()[0] == CURRENT_SCHEMA_VERSION == 77
        cursor = migrated._conn.cursor()
        migrated._apply_v73(cursor)
        migrated._conn.commit()
        assert migrated.list_merge_queue_suspects() == [stop]
        assert migrated.merge_queue_suspect(REPO) == (
            "#9 landed unverified",
            "cd" * 20,
        )
    assert stop.reason == "#9 landed unverified"
    assert stop.kind is None and stop.failure is None
    assert stop.state() == f"main {'cd' * 6} not verified green"
    assert list(tmp_path.glob("dispatch.db.bak-pre-v77-*"))


def test_cli_lists_the_queue_in_order_and_removes_an_entry(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    from typer.testing import CliRunner

    from kew.cli import app

    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    _add(store, gh, 11, "session-a")
    _add(store, gh, 12, "session-b")
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{tmp_path}"\n'
    )
    runner = CliRunner()

    def kew(*args: str) -> Any:
        return runner.invoke(app, ["--project", str(project), "merge", "queue", *args])

    listed = kew("list")
    removed = kew("remove", "11")
    again = kew("remove", "11")
    after = kew("list", "--all")

    assert listed.exit_code == 0, listed.output
    lines = listed.output.splitlines()
    assert lines[0].startswith("#11") and "session-a" in lines[0]
    assert lines[1].startswith("#12") and "session-b" in lines[1]
    assert removed.exit_code == 0 and again.exit_code == 1
    assert "removed" in after.output.splitlines()[0]
    assert [e.pr for e in store.list_merge_queue(REPO)] == [12]


def test_an_entry_removed_during_its_verify_is_not_merged(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)

    def remove() -> None:
        dequeue(store, REPO, pr=11, actor=OPERATOR)

    verify = _Verify(origin.clone, during=remove)
    _runner(store, tmp_path, origin, gh, verify, train=2).run_once()

    assert gh.merged == []
    assert _states(store)[11] == ("removed", "removed by tom")
    assert [e.pr for e in store.list_merge_queue(REPO)] == [12]


# ── the tree that lands is the tree that was verified ──────────────────────


def test_a_stacked_train_verifies_the_tree_squash_merging_lands(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """PR 12 is stacked on PR 11 and deletes 11's file. Squash-merged in order,
    GitHub three-way-merges 12 against main, so main keeps x — the verify must
    read that tree, not the one a chain of merge commits would make."""
    origin.open_pr(11, "pr/x.py")
    _git(origin.work, "checkout", "-q", "-b", "pr-12", "pr-11")
    _git(origin.work, "rm", "-q", "pr/x.py")
    origin.heads[12] = _commit(origin.work, "pr/y.py", "PR 12\n")
    _git(origin.work, "push", "-q", str(origin.origin), "pr-12:refs/pull/12/head")
    _git(origin.work, "checkout", "-q", "main")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify, train=2).run_once()

    assert verify.trees == [["pr/x.py", "pr/y.py"]]
    assert [e.pr for e in done.merged] == [11, 12]
    landed = _git(origin.origin, "ls-tree", "-r", "--name-only", "main").split()
    assert sorted(f for f in landed if f.startswith("pr/")) == verify.trees[0]


def test_a_landed_tree_mismatch_survives_restart_then_repaired_main_resumes(
    tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    gh.extra[11] = "surprise.txt"
    notices: list[NotificationEvent] = []
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as before:
        for pr in (11, 12):
            _add(before, gh, pr)

        first = _runner(
            before, tmp_path, origin, gh, _Verify(origin.clone), 2, notices
        ).run_once()

        assert first.held is not None and "not the verified tree" in first.held
        assert gh.merged == [(11, origin.heads[11])]
        [mismatch] = [
            entry
            for entry in before.list_merge_queue(REPO, include_settled=True)
            if entry.pr == 11
        ]
        actual = _git(origin.origin, "rev-parse", f"{gh.landed[11]}^{{tree}}")
        assert mismatch.state == "landed_mismatch"
        assert mismatch.reason is not None
        assert mismatch.expected_tree in mismatch.reason and actual in mismatch.reason
        assert [entry.pr for entry in before.list_merge_queue(REPO)] == [12]
        assert before.merge_queue_suspect(REPO) == (
            "#11 landed unverified",
            gh.landed[11],
        )

    tree_reads: list[str] = []

    class CountingCandidate:
        def __init__(self, candidate: Any) -> None:
            self.candidate = candidate

        def __getattr__(self, name: str) -> Any:
            return getattr(self.candidate, name)

        def tree_of(self, sha: str) -> str:
            tree_reads.append(sha)
            return self.candidate.tree_of(sha)

    with SqliteRunStore(db) as after:
        verify = _Verify(origin.clone)
        runner = _runner(after, tmp_path, origin, gh, verify, 2, notices)
        runner.candidate = CountingCandidate(runner.candidate)

        held = runner.run_once()

        assert held.held is not None and "waiting for it to move" in held.held
        assert tree_reads == [] and verify.trees == []

        origin._sync_main()
        _git(origin.work, "rm", "surprise.txt")
        _git(origin.work, "commit", "-qm", "repair main")
        _git(origin.work, "push", "-q", str(origin.origin), "main")

        resumed = runner.run_once()

        assert [entry.pr for entry in resumed.merged] == [12]
        assert gh.merged == [(11, origin.heads[11]), (12, origin.heads[12])]
        assert gh.landed[11] not in tree_reads
        assert verify.trees == [["pr/11.py"], ["pr/11.py", "pr/12.py"]]
        assert after.merge_queue_suspect(REPO) is None

        removed = dequeue(after, REPO, pr=11, actor=OPERATOR)
        assert removed is not None and removed.state == "landed_mismatch"
        assert _states(after)[11] == ("removed", "removed by tom")

    [notice] = notices
    assert "not the verified tree" in notice.message and notice.pr_number == 11


def test_a_lone_landed_mismatch_clears_from_needs_you_once_repaired_main_runs(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The only entry lands a tree nobody verified, so the queue is left with
    no active entry — and `kew merge queue run` still resumes it (#2883)."""
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    gh.extra[11] = "surprise.txt"
    _add(store, gh, 11)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)
    config = _config(tmp_path)
    config.project.repo = REPO

    def stopped() -> list[str]:
        found = needs_you_set(store, config)
        return [e.title for e in found.entries if e.type == "merge_queue_suspect"]

    first = runner.run_once()

    assert first.held is not None and "not the verified tree" in first.held
    assert store.list_merge_queue(REPO) == []
    assert stopped() == [f"merge queue stopped — {REPO}"]
    # Nothing verified main red: the row says the landed tree was unverified.
    (row,) = [
        e
        for e in needs_you_set(store, config).entries
        if e.type == "merge_queue_suspect"
    ]
    assert f"main {gh.landed[11][:12]} landed a tree nobody verified" in row.reason
    assert "not the verified tree" in row.reason
    assert "verified red" not in row.reason

    origin._sync_main()
    _git(origin.work, "rm", "surprise.txt")
    _git(origin.work, "commit", "-qm", "repair main")
    _git(origin.work, "push", "-q", str(origin.origin), "main")

    resumed = runner.run_once()

    assert resumed.held is None
    assert verify.trees[-1] == ["pr/11.py"]
    assert store.merge_queue_suspect(REPO) is None
    assert stopped() == []


def test_every_verify_is_told_the_main_the_pass_fetched(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """main moved after the clone last fetched; the verify is handed the main
    the pass fetched, not left to resolve a base of its own."""
    stale = _git(origin.clone, "rev-parse", "origin/main")
    moved = origin.move_main("main moved after the clone fetched\n")
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert verify.bases == [moved] and moved != stale


def test_bisection_builds_every_probe_on_one_base_and_records_it(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/broken.py")
    origin.open_pr(13, "pr/13.py")
    gh = _Gh(origin)
    for pr in (11, 12, 13):
        _add(store, gh, pr)
    base = origin.main()
    moves: list[str] = []

    def move_once() -> None:
        if not moves:
            moves.append(origin.move_main("moved during the train's verify\n"))

    verify = _Verify(origin.clone, during=move_once)
    done = _runner(store, tmp_path, origin, gh, verify, train=3).run_once()

    assert verify.bases == [base] * done.verifies and done.verifies == 4
    state, reason = _states(store)[12]
    assert state == "failed" and reason is not None and base[:12] in reason
    # The rest passed on the pinned base, but main moved: nothing merges.
    assert gh.merged == [] and [e.pr for e in done.requeued] == [11, 13]


# ── main must be where the train expects it, at every merge ────────────────


def test_main_moving_between_two_merges_of_a_train_stops_it(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    gh.after_merge = lambda pr: origin.move_main("someone else merged\n")

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone), 2).run_once()

    assert gh.merged == [(11, origin.heads[11])]
    assert done.held is not None and "#12 and after verify again" in done.held
    [entry] = store.list_merge_queue(REPO)
    assert (entry.pr, entry.state, entry.requeues) == (12, "queued", 0)
    assert store.merge_queue_suspect(REPO) is not None


def test_after_a_partial_train_the_next_pass_verifies_main_first(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin, refuse_merge=12)
    for pr in (11, 12):
        _add(store, gh, pr)
    _runner(store, tmp_path, origin, gh, _Verify(origin.clone), 2).run_once()
    assert gh.merged == [(11, origin.heads[11])]
    assert _states(store)[12][0] == "failed"
    origin.open_pr(13, "pr/13.py")
    _add(store, gh, 13)
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify).run_once()

    # main alone first (no queued PR in it), then #13 on top.
    assert verify.trees == [["pr/11.py"], ["pr/11.py", "pr/13.py"]]
    assert verify.bodies[0] is None
    assert [e.pr for e in done.merged] == [13]
    assert store.merge_queue_suspect(REPO) is None


def test_a_red_main_after_a_partial_train_merges_nothing_and_says_so_once(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    origin.move_main("main as it was\n")
    _git(origin.work, "checkout", "-q", "main")
    _commit(origin.work, "pr/broken.py", "red main\n")
    _git(origin.work, "push", "-q", str(origin.origin), "main")
    store.suspect_merge_queue_main(REPO, "a train stopped after #10 merged")
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, notices=notices)

    first = runner.run_once()
    second = runner.run_once()

    assert gh.merged == [] and [e.pr for e in store.list_merge_queue(REPO)] == [11]
    assert first.held is not None and "is red after" in first.held
    assert second.held is not None and "waiting for it to move" in second.held
    assert len(verify.trees) == 1 and len(notices) == 1
    assert "#10" in notices[0].message


def test_a_red_mains_failure_survives_restart_in_the_row_and_the_list(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verify's failure is said once as an event; the durable row and
    `kew merge queue list` keep it, so a person can see what to repair (#2883)."""
    from typer.testing import CliRunner

    from kew.cli import app

    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    origin.move_main("main as it was\n")
    _git(origin.work, "checkout", "-q", "main")
    red = _commit(origin.work, "pr/broken.py", "red main\n")
    _git(origin.work, "push", "-q", str(origin.origin), "main")
    db = tmp_path / "logs" / "dispatch.db"
    db.parent.mkdir()
    with SqliteRunStore(str(db)) as before:
        _add(before, gh, 11)
        before.suspect_merge_queue_main(REPO, "a train stopped after #10 merged")
        held = _runner(before, tmp_path, origin, gh, _Verify(origin.clone)).run_once()
    assert held.held is not None and "is red after" in held.held

    config = _config(tmp_path)
    config.project.repo = REPO
    with SqliteRunStore(str(db)) as after:
        (row,) = [
            e
            for e in needs_you_set(after, config).entries
            if e.type == "merge_queue_suspect"
        ]
    failure = f"kew verify failed on gcp for main {red[:12]} + nothing (exit 1)"
    assert f"main {red[:12]} verified red ({failure})" in row.reason
    assert "a train stopped after #10 merged" in row.reason

    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{db.parent}"\n'
    )
    listed = CliRunner().invoke(
        app, ["--project", str(project), "merge", "queue", "list"]
    )
    assert listed.exit_code == 0, listed.output
    assert failure in listed.output
    assert "a train stopped after #10 merged" in listed.output


def test_a_main_gone_red_outside_the_queue_fails_no_queued_pr(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12, 13):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12, 13):
        _add(store, gh, pr)
    # Someone merged a red change straight to main, not through the queue.
    origin.move_main("main as it was\n")
    _git(origin.work, "checkout", "-q", "main")
    red = _commit(origin.work, "pr/broken.py", "red main\n")
    _git(origin.work, "push", "-q", str(origin.origin), "main")
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, train=3, notices=notices)

    first = runner.run_once()
    second = runner.run_once()

    assert gh.merged == [] and gh.comments == []
    assert [(e.pr, e.state) for e in store.list_merge_queue(REPO)] == [
        (11, "queued"),
        (12, "queued"),
        (13, "queued"),
    ]
    # The train, its prefixes down to #11, then main alone: red, so held.
    assert verify.trees[-1] == ["pr/broken.py"]
    assert first.held is not None and "is red after #11 failed on it" in first.held
    assert second.held is not None and "waiting for it to move" in second.held
    assert store.merge_queue_suspect(REPO) == (
        "#11 failed on it",
        red,
    )
    assert len(notices) == 1


def test_a_pr_failing_on_a_main_this_runner_landed_verified_is_not_reverified(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/broken.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)

    runner.run_once()
    second = runner.run_once()

    # main landed #11's verified tree, so #12 alone failing on it is #12's.
    assert second.verifies == 1
    assert _states(store)[12][0] == "failed"


# ── claims: a removed entry never merges, a crash is resolved ──────────────


def test_an_entry_cannot_be_removed_once_its_merge_is_claimed(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    refusals: list[str] = []

    def remove_during_merge(pr: int) -> None:
        try:
            dequeue(store, REPO, pr=pr, actor=OPERATOR)
        except QueueError as exc:
            refusals.append(str(exc))

    gh.after_merge = remove_during_merge

    _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert gh.merged == [(11, origin.heads[11])]
    assert _states(store)[11][0] == "merged"
    assert len(refusals) == 1 and "being merged" in refusals[0]


def test_a_claim_a_crash_left_on_a_merged_pr_settles_it_merged(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    [entry] = store.list_merge_queue(REPO)
    expected_base = origin.main()
    expected_tree = _git(origin.work, "rev-parse", f"{origin.heads[11]}^{{tree}}")
    assert store.claim_merge_entry(
        entry.id,
        "host:1",
        expected_base=expected_base,
        expected_tree=expected_tree,
    )
    gh.landed[11] = origin.squash(11)  # the merge landed; the pass died
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify).run_once()

    assert [e.pr for e in done.merged] == [11]
    assert _states(store)[11][0] == "merged"
    # The train it was part of stopped, so main itself was verified.
    assert verify.trees == [["pr/11.py"]] and verify.bodies == [None]


def test_a_claimed_merge_waits_for_githubs_full_sha_then_validates_after_restart(
    tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as before:
        _add(before, gh, 11)
        [entry] = before.list_merge_queue(REPO)
        assert before.claim_merge_entry(
            entry.id,
            "host:1",
            expected_base=origin.main(),
            expected_tree=_git(
                origin.work, "rev-parse", f"{origin.heads[11]}^{{tree}}"
            ),
        )
        landed = origin.squash(11)
        gh.landed[11] = ""

        waiting = _runner(
            before, tmp_path, origin, gh, _Verify(origin.clone)
        ).run_once()

        [claimed] = before.list_merge_queue(REPO)
        assert claimed.state == "merging" and claimed.landed_sha is None
        assert waiting.merged == [] and waiting.held is not None
        assert "full merge commit SHA" in waiting.held
        assert _queued_reports(before) == []

    gh.landed[11] = landed
    with SqliteRunStore(db) as after:
        resumed = _runner(after, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

        assert [entry.pr for entry in resumed.merged] == [11]
        assert _states(after)[11][0] == "merged"
        assert _queued_reports(after) == [(11, claimed.expected_base, landed)]


def test_a_claim_a_crash_left_on_an_unmerged_pr_puts_it_back_in_line(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    [entry] = store.list_merge_queue(REPO)
    assert store.claim_merge_entry(
        entry.id,
        "host:1",
        expected_base=origin.main(),
        expected_tree=_git(origin.work, "rev-parse", f"{origin.heads[11]}^{{tree}}"),
    )

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert [e.pr for e in done.merged] == [11]
    assert gh.merged == [(11, origin.heads[11])]


# ── the PR must merge into the configured repository and base ──────────────


def test_enqueue_refuses_a_pr_that_targets_another_branch(
    store: Any, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    gh.targets[11] = "release"

    with pytest.raises(QueueError, match="targets release, not main"):
        _add(store, gh, 11)

    assert store.list_merge_queue(REPO, include_settled=True) == []


def test_a_pr_retargeted_during_its_verify_is_not_merged(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    verify = _Verify(origin.clone, during=lambda: gh.targets.update({11: "release"}))

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == []
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "targets release" in reason


# ── admission needs kew's own READY verdict ────────────────────────────────


def test_enqueue_refuses_a_head_kew_never_found_ready(
    store: Any, origin: _Origin
) -> None:
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    forge = GhForge(REPO, gh)

    with pytest.raises(QueueError, match="READY"):
        enqueue(store, forge, pr=11, head=head, session="s", actor=OPERATOR)
    _ready(store, head, "NEEDS_CHANGES")
    with pytest.raises(QueueError, match="READY"):
        enqueue(store, forge, pr=11, head=head, session="s", actor=OPERATOR)
    _ready(store, "c" * 40)  # READY, but for other code
    with pytest.raises(QueueError, match="READY"):
        enqueue(store, forge, pr=11, head=head, session="s", actor=OPERATOR)

    assert store.list_merge_queue(REPO, include_settled=True) == []


def test_a_head_whose_later_review_round_needs_changes_is_dropped_unverified(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    _ready(store, head, "NEEDS_CHANGES")
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [] and verify.trees == []
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "READY" in reason


def test_a_review_round_that_needs_changes_during_the_verify_stops_the_merge(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    verify = _Verify(origin.clone, during=lambda: _ready(store, head, "NEEDS_CHANGES"))

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [] and verify.trees == [["pr/11.py"]]
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "READY" in reason


def test_a_pr_marked_draft_after_enqueue_is_dropped_before_verification(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    gh.drafts.add(11)
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [] and verify.trees == []
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "draft" in reason


def test_a_pr_marked_draft_during_verification_is_dropped_before_merge(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    verify = _Verify(origin.clone, during=lambda: gh.drafts.add(11))

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [] and verify.trees == [["pr/11.py"]]
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "draft" in reason


def test_enqueue_refuses_a_head_ready_for_two_nodes(
    store: Any, origin: _Origin
) -> None:
    """Whose acceptance contract binds the head would be a guess."""
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    store.record_review_round("epic", "node-a", head_sha=head, verdict="READY")
    store.record_review_round("epic", "node-b", head_sha=head, verdict="READY")

    with pytest.raises(QueueError, match="ambiguous"):
        enqueue(store, GhForge(REPO, gh), pr=11, head=head, session="s", actor=OPERATOR)


def _record_contract(store: Any, head: str, path: str) -> None:
    """A contract version for the node *head* was reviewed READY as."""
    store.record_acceptance_contract(
        "epic",
        f"node-{head[:8]}",
        path=path,
        sha256="0" * 64,
        author_run_id="run-author",
        commit=head,
    )


def test_queue_runs_every_current_acceptance_contract(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """A train's one verify runs each entry's current contract, in order; an
    entry without a contract adds none, and an amended contract replaces the
    version recorded before it."""
    for pr in (11, 12, 13):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12, 13):
        _add(store, gh, pr)
    _record_contract(store, origin.heads[11], "kew/tests/acceptance/test_11.py")
    _record_contract(store, origin.heads[13], "kew/tests/acceptance/test_13.py")
    _record_contract(store, origin.heads[13], "kew/tests/acceptance/test_13b.py")
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify, train=3).run_once()

    assert [e.pr for e in done.merged] == [11, 12, 13]
    assert verify.contracts == [
        [
            ("kew/tests/acceptance/test_11.py", 1),
            ("kew/tests/acceptance/test_13b.py", 2),
        ]
    ]


def test_contract_amended_during_verify_requeues_train_without_merging(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    head = origin.heads[11]
    _record_contract(store, head, "kew/tests/acceptance/test_11.py")
    verify = _Verify(
        origin.clone,
        during=lambda: _record_contract(
            store, head, "kew/tests/acceptance/test_11_amended.py"
        ),
    )
    notices: list[NotificationEvent] = []

    done = _runner(store, tmp_path, origin, gh, verify, 2, notices).run_once()

    assert gh.merged == [] and [entry.pr for entry in done.requeued] == [11, 12]
    assert verify.contracts == [[("kew/tests/acceptance/test_11.py", 1)]]
    queued = store.list_merge_queue(REPO)
    assert [(entry.pr, entry.state, entry.requeues) for entry in queued] == [
        (11, "queued", 0),
        (12, "queued", 0),
    ]
    assert all(
        entry.reason is not None and "acceptance contract changed" in entry.reason
        for entry in queued
    )
    [notice] = notices
    assert notice.pr_number == 11 and "acceptance contract changed" in notice.message


def test_enqueue_refuses_a_draft_pr(store: Any, origin: _Origin) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    gh.drafts.add(11)

    with pytest.raises(QueueError, match="draft"):
        _add(store, gh, 11)


# ── GitHub's merge answer must say it merged ───────────────────────────────


@pytest.mark.parametrize(
    "answer", ["not-json", "{}", '{"merged": false}', '{"merged": true}']
)
def test_a_merge_answer_that_does_not_say_merged_is_not_believed(
    answer: str,
) -> None:
    def gh(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, answer, "")

    pull = PullState(11, "open", False, "a" * 40, "t", "b", "main", REPO)

    with pytest.raises(QueueError, match="PR #11"):
        GhForge(REPO, gh).merge(pull, "a" * 40)


def test_a_merge_github_did_not_confirm_is_failed_not_recorded_merged(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    gh.answers[11] = '{"merged": false, "message": "not mergeable"}'
    _add(store, gh, 11)

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert done.merged == []
    state, reason = _states(store)[11]
    assert state == "failed" and reason is not None and "did not say" in reason


def test_an_ambiguous_merge_error_holds_the_claim_until_a_delayed_landing(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")

    class LostMergeResponse(_Gh):
        def _merge(
            self, cmd: list[str], pr: int, fields: dict[str, str]
        ) -> subprocess.CompletedProcess[str]:
            if pr == 11:
                return subprocess.CompletedProcess(cmd, 1, "", "connection reset")
            return super()._merge(cmd, pr, fields)

    gh = LostMergeResponse(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    runner = _runner(store, tmp_path, origin, gh, _Verify(origin.clone), train=2)

    waiting = runner.run_once()

    active = store.list_merge_queue(REPO)
    assert waiting.held is not None and "unknown" in waiting.held
    assert [(entry.pr, entry.state) for entry in active] == [
        (11, "merging"),
        (12, "queued"),
    ]
    assert gh.merged == []

    landed = origin.squash(11)
    gh.landed[11] = landed
    resumed = runner.run_once()

    assert [entry.pr for entry in resumed.merged] == [11, 12]
    assert _states(store)[11][0] == "merged"
    assert _queued_reports(store)[0] == (11, active[0].expected_base, landed)


def test_a_head_pushed_after_the_ready_check_drops_a_refused_merge(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    queued = origin.open_pr(11, "pr/11.py")

    class PushThenRefuse(_Gh):
        def _merge(
            self, cmd: list[str], pr: int, fields: dict[str, str]
        ) -> subprocess.CompletedProcess[str]:
            origin.push_pr(pr, "pr/pushed.py")
            return subprocess.CompletedProcess(cmd, 1, "", "head changed")

    gh = PushThenRefuse(origin)
    _add(store, gh, 11)

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert done.merged == []
    state, reason = _states(store)[11]
    assert state == "dropped"
    assert reason is not None and queued[:12] in reason
    assert "queue the new head once it is READY" in reason


def test_a_merged_entry_records_the_commit_main_landed_as(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)

    _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    [event] = store.list_audit_events(limit=None, event=SETTLED_EVENT)
    assert event.payload["merged_sha"] == gh.landed[11] == origin.main()


def test_external_merge_of_changed_head_is_dropped(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """Someone else merged a head pushed after the entry was queued: the
    queue merged nothing, so the entry is dropped, never recorded merged."""
    queued = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    pushed = origin.push_pr(11, "pr/11b.py")
    gh.landed[11] = origin.squash(11)

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert done.merged == []
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "outside the queue" in reason
    [event] = store.list_audit_events(limit=None, event=SETTLED_EVENT)
    assert event.payload["sha"] == pushed and event.payload["queued_sha"] == queued
    assert event.payload["merged_sha"] == gh.landed[11]


def test_an_external_merge_of_the_queued_head_settles_merged(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    gh.landed[11] = origin.squash(11)
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify).run_once()

    assert [e.pr for e in done.merged] == [11] and verify.trees == []
    [event] = store.list_audit_events(limit=None, event=SETTLED_EVENT)
    assert event.payload["merged_sha"] == gh.landed[11]


# ── the lock follows the ledger, not the config ────────────────────────────


def test_two_configs_sharing_one_ledger_share_its_queue_lock(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    verify = _Verify(origin.clone)
    other_config = tmp_path / "elsewhere"
    ledger_lock = store.merge_queue_ledger_lock_path()

    assert ledger_lock is not None
    with merge_queue_lock(ledger_lock) as lease:
        assert lease.acquired
        done = _runner(store, other_config, origin, gh, verify).run_once()

    assert done.held and verify.trees == [] and gh.merged == []
    assert merge_queue_lock_path(_config(other_config)) != ledger_lock


# ── the post-merge mutation report (#2817) ──────────────────────────────────


def _queued_reports(store: Any) -> list[tuple[int, str, str]]:
    return [
        (e.payload["pr"], e.payload["base_sha"], e.payload["ref_sha"])
        for e in reversed(
            store.list_audit_events(limit=None, event=REPORT_QUEUED_EVENT)
        )
    ]


def test_restart_after_merge_resumes_tree_validation_and_queues_one_report(
    tmp_path: Path, origin: _Origin
) -> None:
    class CrashOnFirstTreeRead:
        def __init__(self, candidate: Any) -> None:
            self.candidate = candidate
            self.crashed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.candidate, name)

        def tree_of(self, sha: str) -> str:
            if not self.crashed:
                self.crashed = True
                raise SystemExit("runner stopped after GitHub merged")
            return self.candidate.tree_of(sha)

    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as before:
        _add(before, gh, 11)
        runner = _runner(before, tmp_path, origin, gh, _Verify(origin.clone))
        runner.candidate = CrashOnFirstTreeRead(runner.candidate)

        with pytest.raises(SystemExit, match="after GitHub merged"):
            runner.run_once()

        [landed] = before.list_merge_queue(REPO)
        assert landed.state == "landed"
        assert _queued_reports(before) == []

    with SqliteRunStore(db) as after:
        runner = _runner(after, tmp_path, origin, gh, _Verify(origin.clone))
        resumed = runner.run_once()
        runner.run_once()

        assert [entry.pr for entry in resumed.merged] == [11]
        assert _states(after)[11][0] == "merged"
        assert _queued_reports(after) == [(11, landed.expected_base, landed.landed_sha)]


def test_a_failed_report_enqueue_leaves_landed_for_one_restart_retry(
    store: Any,
    tmp_path: Path,
    origin: _Origin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    append_once = store.append_audit_event_once
    failures = 1

    def fail_report_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failures
        if kwargs.get("event") == REPORT_QUEUED_EVENT and failures:
            failures -= 1
            raise sqlite3.OperationalError("injected report append failure")
        return append_once(*args, **kwargs)

    monkeypatch.setattr(store, "append_audit_event_once", fail_report_once)
    runner = _runner(store, tmp_path, origin, gh, _Verify(origin.clone))

    with pytest.raises(RuntimeError, match="mutation_report_queued"):
        runner.run_once()

    [landed] = store.list_merge_queue(REPO)
    assert landed.state == "landed"
    assert _queued_reports(store) == []

    monkeypatch.setattr(store, "append_audit_event_once", append_once)
    resumed = runner.run_once()
    runner.run_once()

    assert [entry.pr for entry in resumed.merged] == [11]
    assert _states(store)[11][0] == "merged"
    assert _queued_reports(store) == [(11, landed.expected_base, landed.landed_sha)]


def test_each_pr_a_train_merges_is_owed_a_mutation_report_of_its_own_squash(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    for pr in (11, 12):
        origin.open_pr(pr, f"pr/{pr}.py")
    gh = _Gh(origin)
    for pr in (11, 12):
        _add(store, gh, pr)
    main = origin.main()
    report = _Report()
    runner = _runner(store, tmp_path, origin, gh, _Verify(origin.clone), train=2)
    runner.report = report

    runner.run_once()

    # Each report diffs the commit main landed as against the main that PR
    # merged onto, so a train's second PR is scoped to its own squash.
    owed = [(11, main, gh.landed[11]), (12, gh.landed[11], gh.landed[12])]
    assert _queued_reports(store) == owed

    runner.run_reports()

    assert [
        (c["pr_number"], c["base_sha"], c["merged_sha"]) for c in report.calls
    ] == owed
    assert [pr for pr, _ in gh.pr_comments] == [11, 12]
    assert "mutation=completed" in gh.pr_comments[0][1]

    runner.run_reports()
    assert len(report.calls) == 2, "an answered report is not run again"


def test_a_merge_that_lands_another_tree_is_owed_no_mutation_report(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    gh.extra[11] = "surprise.txt"
    _add(store, gh, 11)

    _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert gh.merged == [(11, origin.heads[11])]
    assert _queued_reports(store) == []


def test_owed_reports_wait_while_the_verify_host_cannot_be_reached(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    report = _Report()
    runner = _runner(store, tmp_path, origin, gh, _Verify(origin.clone))
    runner.report = report
    runner.run_once()

    runner.probe = lambda _config: "the instance is gone"
    runner.run_reports()
    assert report.calls == [] and len(_queued_reports(store)) == 1

    runner.probe = lambda _config: None
    runner.run_reports()
    assert [c["pr_number"] for c in report.calls] == [11]


@pytest.mark.parametrize(
    "withdraw",
    [
        pytest.param(
            lambda store, head: _ready(store, head, "NEEDS_CHANGES"),
            id="needs-changes",
        ),
        pytest.param(
            lambda store, head: store.record_review_round(
                "epic", "node-other", head_sha=head, verdict="READY"
            ),
            id="ready-for-a-second-node",
        ),
    ],
)
def test_admission_withdrawn_during_a_failing_train_drops_the_entry_in_bisection(
    store: Any, tmp_path: Path, origin: _Origin, withdraw: Any
) -> None:
    """A review round recorded while the train verifies is re-proved by every
    later verify of the pass — the bisection's included — so the entry it
    withdraws is dropped with the reason, not left to crash the pass."""
    head = origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/broken.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    _add(store, gh, 12)
    fired: list[bool] = []

    def once() -> None:
        if not fired:
            fired.append(True)
            withdraw(store, head)

    verify = _Verify(origin.clone, during=once)

    done = _runner(store, tmp_path, origin, gh, verify, train=2).run_once()

    assert gh.merged == [] and done.merged == []
    states = _states(store)
    state, reason = states[11]
    assert state == "dropped" and reason is not None and "no longer admitted" in reason
    assert states[12][0] == "failed"


# ── each attention says why, and only a person's move is attention (#2982) ──


def _queue_events(store: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        (row.event, row.payload)
        for name in ("merge_queue_attention", "merge_queue_notice")
        for row in store.list_audit_events(event=name)
    ]


def test_a_red_main_is_attention_with_the_main_red_reason(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    origin.move_main("main as it was\n")
    _git(origin.work, "checkout", "-q", "main")
    _commit(origin.work, "pr/broken.py", "red main\n")
    _git(origin.work, "push", "-q", str(origin.origin), "main")
    store.suspect_merge_queue_main(REPO, "a train stopped after #10 merged")
    notices: list[NotificationEvent] = []

    _runner(
        store, tmp_path, origin, gh, _Verify(origin.clone), notices=notices
    ).run_once()

    [(event, payload)] = _queue_events(store)
    assert event == "merge_queue_attention" and payload["reason"] == "main_red"
    assert list(payload)[0] == "reason"
    assert notices[0].severity == "error"
    assert notices[0].metadata["reason"] == "main_red"


def test_an_edited_declaration_is_attention_asking_for_a_re_review(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    gh.bodies[11] = "**Behavior changes**\n- tests/test_old.py::test_x — edited"

    _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    [(event, payload)] = _queue_events(store)
    assert event == "merge_queue_attention"
    assert payload["reason"] == "declaration_edited" and payload["pr_number"] == 11
    assert payload["repo"] == REPO


def test_an_amended_contract_is_a_notice_not_attention(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    head = origin.heads[11]
    _record_contract(store, head, "kew/tests/acceptance/test_11.py")
    verify = _Verify(
        origin.clone,
        during=lambda: _record_contract(
            store, head, "kew/tests/acceptance/test_11_amended.py"
        ),
    )
    notices: list[NotificationEvent] = []

    _runner(store, tmp_path, origin, gh, verify, notices=notices).run_once()

    [(event, payload)] = _queue_events(store)
    assert event == "merge_queue_notice"
    assert payload["reason"] == "contract_amended"
    [notice] = notices
    assert notice.event_type == "merge_queue_notice" and notice.severity == "info"


class _WithdrawOnReread(_Gh):
    """GitHub's re-read of PR 11 before its merge is when PR 12's review
    round lands: after 11's own gone-check, before the contracts' last read."""

    def __init__(self, origin: _Origin, verify: _Verify, store: Any) -> None:
        super().__init__(origin)
        self.verify, self.store, self.fired = verify, store, False

    def pull(self, pr: int) -> dict[str, Any]:
        if pr == 11 and self.verify.trees and not self.fired:
            self.fired = True
            _ready(self.store, self.origin.heads[12], "NEEDS_CHANGES")
        return super().pull(pr)


def test_a_review_withdrawn_at_the_last_contract_read_drops_the_entry(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    origin.open_pr(12, "pr/12.py")
    verify = _Verify(origin.clone)
    gh = _WithdrawOnReread(origin, verify, store)
    _add(store, gh, 11)
    _add(store, gh, 12)
    notices: list[NotificationEvent] = []

    _runner(store, tmp_path, origin, gh, verify, train=2, notices=notices).run_once()

    assert gh.fired and gh.merged == []
    states = _states(store)
    assert states[11][0] == "queued"
    state, reason = states[12]
    assert state == "dropped" and reason is not None and "no longer admitted" in reason
    [(event, payload)] = _queue_events(store)
    assert event == "merge_queue_notice"
    assert payload["reason"] == "review_withdrawn" and payload["pr_number"] == 12
    [notice] = notices
    assert notice.severity == "info"


# ── the advertised recovery and the promised re-review hold (#2982) ─────────


def test_queue_run_verifies_and_clears_a_suspect_without_active_entries(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    gh = _Gh(origin)
    store.suspect_merge_queue_main(REPO, "#10 landed unverified")
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify).run_once()

    assert done.held is None
    assert verify.trees == [[]]
    assert store.merge_queue_suspect(REPO) is None


def test_queue_run_holds_on_a_red_suspect_without_active_entries(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    gh = _Gh(origin)
    _git(origin.work, "checkout", "-q", "main")
    _commit(origin.work, "pr/broken.py", "red main\n")
    _git(origin.work, "push", "-q", str(origin.origin), "main")
    store.suspect_merge_queue_main(REPO, "#10 landed unverified")

    done = _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()

    assert done.held is not None and "is red" in done.held
    assert store.merge_queue_suspect(REPO) is not None


def _drop_for_an_edited_declaration(
    store: Any, tmp_path: Path, origin: _Origin, gh: _Gh
) -> None:
    _add(store, gh, 11)
    gh.bodies[11] = "**Behavior changes**\n- tests/test_old.py::test_x — edited"
    _runner(store, tmp_path, origin, gh, _Verify(origin.clone)).run_once()
    assert _states(store)[11][0] == "dropped"


def test_edited_declaration_requires_a_new_review_round_before_reenqueue(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _drop_for_an_edited_declaration(store, tmp_path, origin, gh)

    with pytest.raises(QueueError, match="new review round"):
        enqueue(
            store,
            GhForge(REPO, gh),
            pr=11,
            head=origin.heads[11],
            session="s1",
            actor=OPERATOR,
        )

    assert [e.state for e in store.list_merge_queue(REPO)] == []


def test_an_edited_declaration_re_reviewed_after_the_drop_is_queued_again(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _drop_for_an_edited_declaration(store, tmp_path, origin, gh)

    _add(store, gh, 11)

    [entry] = store.list_merge_queue(REPO)
    assert entry.pr == 11
    assert entry.behavior_changes == ("tests/test_old.py::test_x",)


def test_a_head_whose_attested_verdict_needs_changes_is_dropped_unverified(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The one merge verdict rule (#3196): under a READY round, an attested
    NEEDS_CHANGES for the queued head still stops its merge."""
    from kew.config import ReviewConfig
    from kew.review_audit import emit_review_verdict

    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add(store, gh, 11)
    emit_review_verdict(
        store,
        ReviewConfig(),
        pr_number=11,
        item_id=f"node-{head[:8]}",
        head_sha=head,
        review_round=1,
        verdict="NEEDS_CHANGES",
    )
    verify = _Verify(origin.clone)

    _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [] and verify.trees == []
    state, reason = _states(store)[11]
    assert state == "dropped" and reason is not None and "NEEDS_CHANGES" in reason


def test_a_ready_naming_only_the_full_suite_is_verified_and_merged(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The queue's gate is a `kew verify` of the train: it runs the full suite
    the review sandbox could not, so no hold is added (#3421)."""
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    _add_with_unchecked(store, gh, 11, tmp_path, ["full_suite"])
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify).run_once()

    assert [entry.pr for entry in done.merged] == [11]
    assert gh.merged == [(11, head)]
    assert store.list_epic_holds("epic") == []


@pytest.mark.parametrize("kind", ["docker", "network", "host", "other"])
def test_a_ready_naming_a_person_check_is_held_until_its_head_is_released(
    store: Any, tmp_path: Path, origin: _Origin, kind: str
) -> None:
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    item_id = _add_with_unchecked(store, gh, 11, tmp_path, [kind])
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, notices=notices)

    first = runner.run_once()

    reason = merge_gate_hold_reason(head, f"review sandbox could not run: {kind}")
    assert first.held == reason
    assert verify.bases == [] and gh.merged == []
    [hold] = store.list_epic_holds("epic")
    assert (hold.item_id, hold.reason) == (item_id, reason)
    assert len(notices) == 1 and "human releases this head" in notices[0].message

    released = store.remove_epic_hold(
        "epic",
        item_id,
        receipt_key=merge_gate_ack_key("epic", item_id),
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )
    assert released is not None

    second = runner.run_once()

    assert [entry.pr for entry in second.merged] == [11]
    assert gh.merged == [(11, head)]


def test_a_ready_naming_a_browser_check_waits_for_a_browser_checked_verify(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """A console change: a ``kew verify`` of it runs the browser ui-check."""
    head = origin.open_pr(11, "frontend/src/chat.ts")
    gh = _Gh(origin)
    _add_with_unchecked(store, gh, 11, tmp_path, ["browser"])
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, notices=notices)

    first = runner.run_once()

    assert first.held == MERGE_VERIFY_BROWSER_UNCHECKED
    assert verify.bases == [] and gh.merged == []
    assert store.list_epic_holds("epic") == [], "no hold a person must release"
    assert "ran the browser check" in notices[0].message

    emit(
        store,
        VERIFY_EVENT,
        actor=Actor(type="policy", id="verify-host"),
        payload={
            "ref_sha": "d" * 40,
            "ref_parents": [head, "e" * 40],
            "ui_check": True,
            "ui_check_modules": ["chat"],
            "outcome": "passed",
        },
    )
    second = runner.run_once()

    assert [entry.pr for entry in second.merged] == [11]
    assert gh.merged == [(11, head)]


def _release(store: Any, item_id: str) -> None:
    """Exactly what `kew epic unhold` does to release a hold (cli/epic.py)."""
    released = store.remove_epic_hold(
        "epic",
        item_id,
        receipt_key=merge_gate_ack_key("epic", item_id),
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )
    assert released is not None


def test_a_browser_check_no_verify_of_the_diff_runs_is_held_for_a_person(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The diff touches nothing under ``frontend/``: no ``kew verify`` of it
    ever runs ui-check, so a wait would never end. A person runs the browser
    check, and the head's receipt releases it."""
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    item_id = _add_with_unchecked(store, gh, 11, tmp_path, ["browser"])
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify, notices=notices)

    first = runner.run_once()

    reason = merge_gate_hold_reason(head, "review sandbox could not run: browser")
    assert first.held == reason
    assert verify.bases == [] and gh.merged == []
    [hold] = store.list_epic_holds("epic")
    assert (hold.item_id, hold.reason) == (item_id, reason)
    assert "human releases this head" in notices[0].message

    _release(store, item_id)
    runner.run_once()

    assert gh.merged == [(11, head)]


def test_an_accepted_needs_changes_naming_docker_is_held_until_released(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The queue admits the stop rule's accepted residuals (#2112) like a
    READY, so what that round's reviewer could not run is held the same."""
    head = origin.open_pr(11, "pr/11.py")
    gh = _Gh(origin)
    item_id = _add_with_unchecked(store, gh, 11, tmp_path, ["docker"], accepted=True)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)

    first = runner.run_once()

    reason = merge_gate_hold_reason(head, "review sandbox could not run: docker")
    assert first.held == reason
    assert verify.bases == [] and gh.merged == []

    _release(store, item_id)
    runner.run_once()

    assert gh.merged == [(11, head)]


def test_one_hold_names_the_gate_edit_and_the_unrun_check(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The person who releases the head sees every risk the release answers,
    and one release answers them all."""
    head = origin.open_pr(11, "kew/scripts/verify.sh")
    gh = _Gh(origin)
    item_id = _add_with_unchecked(store, gh, 11, tmp_path, ["docker"])
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)

    first = runner.run_once()

    reason = merge_gate_hold_reason(
        head, "kew/scripts/verify.sh; review sandbox could not run: docker"
    )
    assert first.held == reason
    [hold] = store.list_epic_holds("epic")
    assert hold.reason == reason and verify.bases == [] and gh.merged == []

    _release(store, item_id)
    runner.run_once()

    assert gh.merged == [(11, head)]
    assert store.list_epic_holds("epic") == []


@pytest.mark.parametrize("kind", ["docker", "browser"])
def test_a_pr_held_for_a_person_does_not_block_the_pr_behind_it(
    store: Any, tmp_path: Path, origin: _Origin, kind: str
) -> None:
    """The queue steps past a PR a person must release, records why on it,
    and verifies and merges the next queued PR in the same pass."""
    held_head = origin.open_pr(11, "pr/11.py")
    clean_head = origin.open_pr(12, "pr/12.py")
    gh = _Gh(origin)
    item_id = _add_with_unchecked(store, gh, 11, tmp_path, [kind])
    _add(store, gh, 12)
    notices: list[NotificationEvent] = []
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify, notices=notices).run_once()

    assert [entry.pr for entry in done.merged] == [12]
    assert gh.merged == [(12, clean_head)]
    assert verify.trees == [["pr/12.py"]]
    assert done.held is None
    reason = merge_gate_hold_reason(held_head, f"review sandbox could not run: {kind}")
    [hold] = store.list_epic_holds("epic")
    assert (hold.item_id, hold.reason) == (item_id, reason)
    assert [n.pr_number for n in notices if reason in n.message] == [11]
    assert _states(store)[11] == ("queued", None)


def test_a_pr_waiting_for_a_browser_check_does_not_block_the_pr_behind_it(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    origin.open_pr(11, "frontend/src/chat.ts")
    clean_head = origin.open_pr(12, "pr/12.py")
    gh = _Gh(origin)
    _add_with_unchecked(store, gh, 11, tmp_path, ["browser"])
    _add(store, gh, 12)
    verify = _Verify(origin.clone)

    done = _runner(store, tmp_path, origin, gh, verify).run_once()

    assert gh.merged == [(12, clean_head)]
    assert done.held is None
    assert store.list_epic_holds("epic") == []
    assert _states(store)[11] == ("queued", None)


def test_the_console_s_ui_check_does_not_answer_a_www_browser_check(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """The PR edits the console AND kew.dev; a passing ui-check verify of it is
    on record, but no ``kew verify`` runs www's ``npm run browser-check``."""
    origin.open_pr(11, "frontend/src/chat.ts")
    head = origin.push_pr(11, "www/index.html")
    gh = _Gh(origin)
    item_id = _add_with_unchecked(store, gh, 11, tmp_path, ["browser"])
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor(type="policy", id="verify-host"),
        payload={
            "ref_sha": head,
            "ref_parents": [],
            "ui_check": True,
            "ui_check_modules": None,
            "outcome": "passed",
        },
    )
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)

    first = runner.run_once()

    reason = merge_gate_hold_reason(head, "review sandbox could not run: browser")
    assert first.held == reason
    assert verify.bases == [] and gh.merged == []

    _release(store, item_id)
    runner.run_once()

    assert gh.merged == [(11, head)]


@pytest.mark.parametrize("suite", ["full", "tiered"])
def test_a_ready_naming_the_full_suite_is_verified_on_the_full_suite(
    store: Any, tmp_path: Path, origin: _Origin, suite: str
) -> None:
    """The queue waives a ``full_suite`` entry only because its verify runs the
    suite, so it tells the verify to, however ``[verify] suite`` is set: a
    tiered run of selected tests answers nothing about the rest."""
    named_head = origin.open_pr(11, "pr/11.py")
    clean_head = origin.open_pr(12, "pr/12.py")
    gh = _Gh(origin)
    _add_with_unchecked(store, gh, 11, tmp_path, ["full_suite"])
    _add(store, gh, 12)
    verify = _Verify(origin.clone)
    runner = _runner(store, tmp_path, origin, gh, verify)
    runner.config.verify.suite = suite

    runner.run_once()
    runner.run_once()

    assert gh.merged == [(11, named_head), (12, clean_head)]
    assert verify.full == [True, False]
    assert store.list_epic_holds("epic") == []


def test_a_release_answers_only_what_its_hold_named(
    store: Any, tmp_path: Path, origin: _Origin
) -> None:
    """A gate edit released on H; a later re-review of the same H names a
    Docker check nobody released. The head is held again under one reason
    naming both, and the next release lets it merge."""
    head = origin.open_pr(11, "kew/scripts/verify.sh")
    gh = _Gh(origin)
    _add(store, gh, 11)
    item_id = f"node-{head[:8]}"
    runner = _runner(store, tmp_path, origin, gh, _Verify(origin.clone))
    assert runner.run_once().held == merge_gate_hold_reason(
        head, "kew/scripts/verify.sh"
    )
    _release(store, item_id)
    record_reviewed(
        store,
        tmp_path,
        epic_ref="epic",
        item_id=item_id,
        head=head,
        kinds=["docker"],
        repo=REPO,
    )

    again = runner.run_once()

    reason = merge_gate_hold_reason(
        head, "kew/scripts/verify.sh; review sandbox could not run: docker"
    )
    assert again.held == reason and gh.merged == []
    [hold] = store.list_epic_holds("epic")
    assert hold.reason == reason

    _release(store, item_id)
    runner.run_once()

    assert gh.merged == [(11, head)]
