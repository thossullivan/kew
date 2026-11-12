"""Tests for the serial merge queue that verifies main + head (#2148).

The proof the issue asks for is `test_two_ready_siblings_...`: two READY siblings
whose combined tree fails. One merges; the other is sent back with the failing
gate named. It runs against REAL git repositories (a bare origin, a working
clone, two pushed branches) and a REAL run store, because the failure being
closed is precisely that each head is green alone — only a real merge of a real
base into a real head can show it. Only GitHub (`gh`) is faked, and the fake
`gh pr merge` performs the squash merge for real, so the second sibling is
verified against a base that actually contains the first.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.gate_path_repo import IGNORED_SUBMODULE, add_ignored_submodule
from support.unchecked_reviews import record_reviewed

from kew import subprocess_utils
from kew.audit import Actor, emit
from kew.config import DispatchConfig
from kew.epic_holds import (
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    holds_node_out_of_frontier,
    merge_gate_ack_key,
    merge_gate_hold_reason,
    merge_gate_reviewed_head,
)
from kew.graph.adapters.linear_facts import LinearFactsReader
from kew.graph.driver import merge_queue as _mq
from kew.graph.driver import tick as _tick
from kew.graph.driver.facts import NodeFacts, RepositoryPermission
from kew.graph.driver.lock import (
    HEARTBEAT_STALE_SECONDS,
    lock_holder,
    single_driver_lock,
)
from kew.graph.driver.merge import MergeVerdict, _verified_merge, merge_pass
from kew.graph.driver.merge_queue import (
    MERGE_QUEUE_BUSY,
    MERGE_QUEUE_LOCK_UNUSABLE,
    MERGE_VERIFY_BASE_UNMERGEABLE,
    MERGE_VERIFY_BROWSER_UNCHECKED,
    MERGE_VERIFY_CHECKOUT_REFUSED,
    MERGE_VERIFY_FAILED,
    MERGE_VERIFY_HEAD_MOVED,
    MERGE_VERIFY_HEAD_UNREADABLE,
    MERGE_VERIFY_NO_BRANCH,
    MERGE_VERIFY_TIMEOUT,
    MERGE_VERIFY_TIMEOUT_SECONDS,
    MERGE_VERIFY_UNCONFIGURED,
    MERGE_VERIFY_UNCONTAINED,
    MERGE_VERIFY_UNRUNNABLE,
    keep_driver_lock_alive,
    merge_queue_lock_path,
    merge_queue_verifier,
)
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import _is_awaiting_merge, _lock_keepalive
from kew.loops.verification import (
    HOST_VERIFICATION_POSTURE,
    CommandCondition,
    ConditionResult,
)
from kew.merge_verify import VERIFY_EVENT
from kew.review_checkout import CHECKOUT_BUSY, CHECKOUT_DIRTY
from kew.review_contract import parse_verdict_sidecar

store = _fx.store
driver_env = _fx.driver_env

REPO = "o/r"
BASE = "main"
EPIC_REF = "epic/1"

# The gate that only a combined tree can fail: once api.txt requires field_x,
# EVERY fixture must carry it. Each sibling satisfies it alone.
GATE = """#!/bin/sh
grep -q field_x api.txt || exit 0
for f in fixtures/*; do
  grep -q field_x "$f" || { echo "fixture gate: $f is missing field_x"; exit 1; }
done
"""


@pytest.fixture(autouse=True)
def _tools() -> Any:
    """The tools a real verification needs: git, the shell, and `timeout`."""
    subprocess_utils._register_optional_tool("git")
    subprocess_utils._register_optional_tool("sh")
    timeout_cmd = subprocess_utils.find_timeout_cmd()
    if timeout_cmd:
        subprocess_utils._register_optional_tool(timeout_cmd)
    yield


def _wait(seconds: float) -> None:
    """Sleep for real. NOT ``time.sleep``: ``conftest`` no-ops it suite-wide
    (``kew.selector.time`` is the ``time`` module), and these assertions are
    about a background thread actually getting to run."""
    threading.Event().wait(seconds)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(work: Path, message: str) -> str:
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", message, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _sibling_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A bare origin with ``main`` and two sibling branches, each green alone."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gate.sh").write_text(GATE)
    (work / "api.txt").write_text("required: none\n")
    (work / "fixtures").mkdir()
    (work / "fixtures" / "one.txt").write_text("{}\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    # Sibling A requires a new field and updates the fixture it knows about.
    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    (work / "api.txt").write_text("required: field_x\n")
    (work / "fixtures" / "one.txt").write_text("{field_x}\n")
    heads = {"101": _commit(work, "require field_x")}
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)

    # Sibling B adds a fixture — green against main, red against main+A.
    _git("checkout", "-q", BASE, cwd=work)
    _git("checkout", "-qb", "agent/issue-102", cwd=work)
    (work / "fixtures" / "two.txt").write_text("{}\n")
    heads["102"] = _commit(work, "add a second fixture")
    _git("push", "-q", "origin", "agent/issue-102", cwd=work)

    _git("checkout", "-q", "--detach", cwd=work)
    return work, heads


def _config(tmp_path: Path, *, verify: str = "sh gate.sh") -> DispatchConfig:
    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path / "wt")
    config.logging.dir = str(tmp_path / "logs")
    # The queue requires an EXPLICIT verify command (#2184): it no longer
    # falls back to `commands.ci`, which is usually a narrower gate.
    config.commands.verify_loop = verify
    config.epic.merge_queue = True
    # Both prerequisites the config validator enforces for the opt-in: the
    # review stage that remediates a red tree, and (with no container runtime
    # on this machine) the operator flag that produces the host fallback
    # posture `resolve_command_verification_admission` hands back (#2184).
    config.review.adversarial = True
    config.sandbox.dangerously_skip_containment = True
    return config


def _facts(issue_num: int, *, pr_number: int, state: str = "OPEN") -> NodeFacts:
    return NodeFacts(
        issue_num=issue_num,
        state=state,
        state_reason=None,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=None,
        review_decision="APPROVED",
        merge_state_status="CLEAN",
        head_oid=None,
        pr_pushed_at=None,
    )


def _pr(number: int, *, branch: str, head: str) -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": "CLEAN",
        "headRefOid": head,
        "headRefName": branch,
        "commits": [],
        "latestReviews": [
            {
                "state": "APPROVED",
                # The governed-review invariant needs the approval to postdate
                # the READY round, which these tests record at "now".
                "submittedAt": "2099-01-01T00:00:00Z",
                "author": {
                    "login": "alice",
                    "is_bot": False,
                    "__typename": "User",
                },
            }
        ],
        "author": {"login": "agent"},
    }


class _FakeReader:
    """Minimal GitHubFactsReader over a fixed node → (pr, branch, head) map."""

    def __init__(self, nodes: dict[str, dict[str, Any]]) -> None:
        self.nodes = nodes
        self.issue_states = {node: "OPEN" for node in nodes}
        self.checks: tuple[str, str] = ("success", "CI green (2 checks)")

    def _by_pr(self, pr_number: int) -> dict[str, Any]:
        return next(n for n in self.nodes.values() if n["pr"] == pr_number)

    def issue_facts(self, issue_num: int) -> NodeFacts:
        node = self.nodes[str(issue_num)]
        return _facts(
            issue_num, pr_number=node["pr"], state=self.issue_states[str(issue_num)]
        )

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        node = self._by_pr(pr_number)
        return _pr(pr_number, branch=node["branch"], head=node["head"])

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        return self.pr_facts(pr_number)

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return self.checks

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return pr.get("mergeStateStatus") == "CLEAN"

    def repository_permission(self, login: str) -> RepositoryPermission | None:
        return RepositoryPermission(login, "User", "write")


def _make_run(work: Path, reader: _FakeReader) -> tuple[list[list[str]], Any]:
    """A `gh` fake whose ``pr merge`` really squash-merges into origin/main."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "git":
            return subprocess.run(cmd, capture_output=True, text=True, check=False)
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "merge"]:
            node = reader._by_pr(int(cmd[3]))
            _git("fetch", "-q", "origin", cwd=work)
            _git("checkout", "-q", BASE, cwd=work)
            _git("merge", "-q", "--squash", f"origin/{node['branch']}", cwd=work)
            _commit(work, f"squash {node['branch']}")
            _git("push", "-q", "origin", BASE, cwd=work)
            _git("checkout", "-q", "--detach", cwd=work)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "issue", "close"]:
            reader.issue_states[cmd[3]] = "CLOSED"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return calls, fake_run


def _stub_verifier(*, on_enter: Any = None) -> Any:
    """A green stand-in for the queue's verifier seam.

    A context manager, like the real one: the merge pass performs the merge
    INSIDE the lease, so a seam that is a plain call would not exercise the code
    under test at all. *on_enter* runs as the lease opens — the window a real
    verification occupies.
    """

    @contextmanager
    def _verify(**_kwargs: Any) -> Any:
        if on_enter is not None:
            on_enter()
        yield _mq.VERIFIED

    return _verify


def _ready(store: Any, item_id: str, head: str) -> None:
    store.record_review_round(
        EPIC_REF, item_id, head_sha=head, verdict="READY", review_mode="full"
    )


def _verdicts(
    nodes: list[str],
    *,
    config: DispatchConfig,
    store: Any,
    reader: _FakeReader,
    run: Any,
    work: Path,
    blocking_review: bool = True,
) -> Any:
    return merge_pass(
        nodes,
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        run=run,
        blocking_review=blocking_review,
        gated_checks=[],
        merge_verify=merge_queue_verifier(
            config,
            store,
            epic_ref=EPIC_REF,
            repo=REPO,
            base=BASE,
            repo_root=work,
        ),
    )


def _two_siblings(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[DispatchConfig, _FakeReader, Any, Path, list[list[str]]]:
    work, heads = _sibling_repo(tmp_path)
    monkeypatch.chdir(work)
    config = _config(tmp_path)
    reader = _FakeReader(
        {
            "101": {"pr": 501, "branch": "agent/issue-101", "head": heads["101"]},
            "102": {"pr": 502, "branch": "agent/issue-102", "head": heads["102"]},
        }
    )
    _ready(store, "101", heads["101"])
    _ready(store, "102", heads["102"])
    calls, run = _make_run(work, reader)
    return config, reader, run, work, calls


def test_two_ready_siblings_merge_one_and_send_the_other_back_with_the_gate(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)

    verdicts = _verdicts(
        ["101", "102"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert [(v.node_id, v.action) for v in verdicts] == [
        ("101", "merged"),
        ("102", "skip"),
    ]
    assert verdicts[1].reason.startswith(MERGE_VERIFY_FAILED)
    # Only the verified sibling reached `gh pr merge`.
    assert [c[3] for c in calls if c[:3] == ["gh", "pr", "merge"]] == ["501"]

    # The refusal is a verdict the driver can remediate, against the head that
    # was verified, with the failing gate named in its evidenced finding.
    latest = store.latest_review_round("102")
    assert (latest.verdict, latest.head_sha) == (
        "NEEDS_CHANGES",
        reader.nodes["102"]["head"],
    )
    parsed = parse_verdict_sidecar(Path(latest.sidecar_path).read_bytes())
    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.severity for f in parsed.findings] == ["high"]
    assert "fixture gate: fixtures/two.txt is missing field_x" in (
        parsed.findings[0].summary
    )
    assert parsed.findings[0].evidence_command == "sh gate.sh"
    # Both decisions are attested, with the facts in the payload.
    attested = {
        event.item_id: (event.payload or {})
        for event in store.list_audit_events(event="merge_verify")
    }
    assert attested["101"]["result"] == "pass"
    assert attested["102"]["result"] == "fail"
    assert attested["102"]["gate"] == "sh gate.sh"
    assert attested["102"]["base"] == BASE


#: A proxy whose *path* is the secret — the shape only a launch-scoped redaction
#: covers, since a bare path segment is not credential-shaped on its own.
_PROXY_WITH_A_SECRET_PATH = "http://proxy.internal:3128/pr0xypathsecret/"


def test_the_verify_commands_evidence_cannot_retain_a_proxy_path_secret(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CommandCondition.check()` wraps every mode in `proxy_redaction_scope`
    before any evidence is sanitized, and only inside that scope do a proxy URL's
    path, query and fragment count as credential forms. The queue reaches
    `check_directory` directly, and its evidence is *retained*: it is written to
    a review sidecar under `logging.dir` and handed to a remediation agent — so
    the scope has to hold there too."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    monkeypatch.setenv("https_proxy", _PROXY_WITH_A_SECRET_PATH)
    secret = _PROXY_WITH_A_SECRET_PATH.rsplit("/", 2)[1]
    # The command text carries no secret — the *output* does, and only the path
    # segment of it: a whole proxy URL is credential-shaped outside the scope too.
    config.commands.verify_loop = "printenv https_proxy | cut -d/ -f4; exit 1"

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].reason.startswith(MERGE_VERIFY_FAILED)
    latest = store.latest_review_round("101")
    sidecar = Path(latest.sidecar_path).read_bytes()
    assert secret.encode() not in sidecar
    assert b"<https_proxy redacted>" in sidecar
    # And the same text reaches the remediation agent through the parsed finding.
    finding = parse_verdict_sidecar(sidecar).findings[0]
    assert secret not in finding.summary
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_the_failed_sibling_is_no_longer_mergeable_on_the_next_pass(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded round holds the merge gate closed until remediation lands."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    _verdicts(
        ["101", "102"], config=config, store=store, reader=reader, run=run, work=work
    )
    calls.clear()

    again = _verdicts(
        ["102"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert (again[0].action, again[0].reason) == ("skip", "review-not-ready")
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_head_that_moves_during_verification_is_not_merged(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)

    def _move_head() -> None:
        reader.nodes["101"]["head"] = "f" * 40

    moving_verify = _stub_verifier(on_enter=_move_head)

    verdicts = merge_pass(
        ["101"],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        run=run,
        blocking_review=False,
        gated_checks=[],
        merge_verify=moving_verify,
    )

    assert (verdicts[0].action, verdicts[0].reason) == ("skip", MERGE_VERIFY_HEAD_MOVED)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_busy_queue_holds_the_candidate_instead_of_merging_it(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    lock_path = merge_queue_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        verdicts = _verdicts(
            ["101"], config=config, store=store, reader=reader, run=run, work=work
        )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(MERGE_QUEUE_BUSY)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_queue_lock_the_filesystem_refuses_is_not_reported_as_busy(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied `flock` is normally contention, which the tick treats as another
    driver working and retries forever. An errno that is NOT contention — a
    filesystem without locking, a permission error — is a lock no tick can ever
    take, so it must carry its own reason and reach the operator as a stall."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    real_flock = fcntl.flock

    def no_locks(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, "No locks available")
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", no_locks)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(MERGE_QUEUE_LOCK_UNUSABLE)
    assert not _mq.is_queue_contention(verdicts[0].reason)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_head_that_moved_before_the_tree_was_cut_is_awaiting_re_review(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout primitive refuses `head_mismatch` when the head this tick
    classified is no longer what `origin/<branch>` resolves to. That is the same
    benign "the head moved" as a head that moves mid-verification, not a refused
    checkout an operator must inspect: it must read as awaiting re-review, or a
    routine push during a tick ends `kew epic run` with a repair code."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    # This tick's PR read is one push behind the branch it names.
    reader.nodes["101"]["head"] = "0" * 40

    verdicts = _verdicts(
        ["101"],
        config=config,
        store=store,
        reader=reader,
        run=run,
        work=work,
        blocking_review=False,
    )

    assert (verdicts[0].action, verdicts[0].reason) == ("skip", MERGE_VERIFY_HEAD_MOVED)
    assert _queue_signal(verdicts[0]) is TerminalSignal.AWAITING_MERGES
    assert store.latest_review_round("101").verdict == "READY"  # no round recorded
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def _queue_is_free(lock_path: Path) -> bool:
    """Whether a second driver could take the queue lock right now."""
    with _mq.merge_queue_lock(lock_path) as lease:
        return lease.acquired


def test_the_queue_lock_is_still_held_while_the_verified_merge_runs(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Released at the verdict, the lock left the merge itself unserialized: a
    second epic driver ticking in that window verified a combined tree built on
    a base that does not yet contain the merge about to land — exactly the
    cross-epic invalidation the repo-wide lock exists to prevent. The lease must
    span the merge, and end with it."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    lock_path = merge_queue_lock_path(config)
    free_during_merge: list[bool] = []

    def probing_run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[:3] == ["gh", "pr", "merge"]:
            free_during_merge.append(_queue_is_free(lock_path))
        return run(cmd, **kwargs)

    verdicts = _verdicts(
        ["101"],
        config=config,
        store=store,
        reader=reader,
        run=probing_run,
        work=work,
    )

    assert verdicts[0].action == "merged"
    assert free_during_merge == [False], "the queue was open during the merge"
    # And the lease ends with the merge — it is a queue, not a permanent hold.
    assert _queue_is_free(lock_path) is True


@pytest.mark.parametrize(
    ("fresh_title", "subject"),
    [
        pytest.param("The corrected  title", "The corrected title", id="corrected"),
        pytest.param("", "The title kew classified", id="blank-on-re-read"),
    ],
)
def test_a_title_corrected_during_the_verification_is_the_squash_subject(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    fresh_title: str,
    subject: str,
) -> None:
    """A verification runs for minutes: the title the head re-read after it
    returns is the one that lands on main, not the classification's (#3187)."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    facts = reader.pr_facts
    reader.pr_facts = lambda n: {**facts(n), "title": "The title kew classified"}  # type: ignore[method-assign]
    reader.fresh_pr_facts = lambda n: {**facts(n), "title": fresh_title}  # type: ignore[method-assign]

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"
    (merge,) = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge[-2:] == ["--subject", f"{subject} (#{merge[3]})"]


def test_no_verify_command_holds_the_candidate_rather_than_passing_it(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.commands.ci = ""
    config.commands.verify_loop = ""

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_UNCONFIGURED)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_pr_with_no_head_branch_is_never_merged_unverified(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    reader.nodes["101"]["branch"] = ""

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert (verdicts[0].action, verdicts[0].reason) == ("held", MERGE_VERIFY_NO_BRANCH)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_conflict_with_the_base_is_sent_back_as_a_remediable_verdict(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    # Move main so it edits the same line sibling A edits.
    _git("checkout", "-q", BASE, cwd=work)
    (work / "api.txt").write_text("required: something-else\n")
    _commit(work, "conflicting base change")
    _git("push", "-q", "origin", BASE, cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "skip"
    assert verdicts[0].reason == f"{MERGE_VERIFY_FAILED}: merge with {BASE}"
    latest = store.latest_review_round("101")
    assert latest.verdict == "NEEDS_CHANGES"
    parsed = parse_verdict_sidecar(Path(latest.sidecar_path).read_bytes())
    assert "api.txt" in parsed.findings[0].summary
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_the_queue_leaves_no_combined_tree_behind(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree carrying base commits the head does not would change what a later
    review round reads."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)

    _verdicts(
        ["101", "102"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert not list((Path(config.branches.worktree_base) / "review").glob("pr-*/.git"))


def test_the_queue_is_off_unless_the_epic_opts_in(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the opt-in the merge pass merges exactly as it did before, so the
    combined-tree failure lands — the behaviour the flag exists to change."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    assert DispatchConfig().epic.merge_queue is False

    verdicts = merge_pass(
        ["101", "102"],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        run=run,
        blocking_review=True,
        gated_checks=[],
    )

    assert [v.action for v in verdicts] == ["merged", "merged"]


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("timed_out", MERGE_VERIFY_TIMEOUT), ("unrunnable", MERGE_VERIFY_UNRUNNABLE)],
)
def test_a_gate_that_could_not_run_holds_and_reports_no_pr_defect(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    expected: str,
) -> None:
    """A verify command that never completed proves nothing about the PR: hold
    the merge, and do NOT send a finding back for an agent to remediate."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ConditionResult(
            met=False, evidence="never finished", **{flag: True}
        ),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(expected)
    assert store.latest_review_round("101").verdict == "READY"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


@pytest.mark.parametrize(
    ("action", "reason", "awaiting"),
    [
        ("skip", f"{MERGE_VERIFY_FAILED}: sh gate.sh", True),
        ("skip", MERGE_VERIFY_HEAD_MOVED, True),
        ("held", f"{MERGE_VERIFY_UNCONFIGURED}: set commands.verify_loop", False),
        ("held", f"{MERGE_VERIFY_UNCONTAINED}: #1444 refusal", False),
        ("held", MERGE_VERIFY_HEAD_UNREADABLE, False),
    ],
)
def test_a_sent_back_candidate_is_awaiting_re_review_not_a_stall(
    action: str, reason: str, awaiting: bool
) -> None:
    """A recorded round is remediation waiting to happen, so it must not misfire
    the blast-radius alarm — while a queue an operator has to fix still does.
    (Contention is neither: it goes through the *held* reading below.)"""
    assert _is_awaiting_merge(MergeVerdict("101", action, reason)) is awaiting


class _NoGraph:
    """No queue verdict has a blast radius, so none may read the graph."""

    def transitive_dependents(self, node_id: str) -> set[str]:
        raise AssertionError("a merge-queue verdict needs no blast radius")


def _queue_signal(verdict: MergeVerdict) -> TerminalSignal:
    """The signal `kew epic run` reads for an epic whose one node got *verdict*.

    The real composition, not a restatement of it: `_classify_verdicts` decides
    whether a *held* verdict is live, `_is_awaiting_merge` decides whether a
    *skip* one is a benign merge wait, and `_terminal_signal` turns both into the
    tri-state — wired exactly as `_run_tick` wires them for a tick that
    dispatched nothing, has nothing in flight, and is not done.
    """
    outcome = _tick._classify_verdicts(
        [verdict],
        graph=_NoGraph(),  # type: ignore[arg-type]
        holds_after={},
        now=datetime.now(UTC),
        stall_days=3,
    )
    assert not outcome.stuck_by_id
    return _tick._terminal_signal(
        remaining=1,
        dispatched=[],
        in_flight=[],
        held_live=outcome.held_live,
        awaiting_merges=_is_awaiting_merge(verdict),
    )


@pytest.mark.parametrize(
    ("action", "reason", "signal"),
    [
        # Contention: another live process holds a flock — the repo-wide queue
        # lock, or this PR's review checkout. Nothing to repair; the holder
        # releases it (or dies) and the next tick re-evaluates.
        (
            "held",
            f"{MERGE_QUEUE_BUSY}: /tmp/merge-queue.lock is held",
            TerminalSignal.PROGRESSING,
        ),
        (
            "held",
            f"{MERGE_VERIFY_CHECKOUT_REFUSED}: {CHECKOUT_BUSY}",
            TerminalSignal.PROGRESSING,
        ),
        # Awaiting re-review: a recorded round, or a head that moved.
        (
            "skip",
            f"{MERGE_VERIFY_FAILED}: sh gate.sh",
            TerminalSignal.AWAITING_MERGES,
        ),
        ("skip", MERGE_VERIFY_HEAD_MOVED, TerminalSignal.AWAITING_MERGES),
        # Only an operator can clear these — STALLED is the signal that says so.
        (
            "held",
            f"{MERGE_VERIFY_UNCONFIGURED}: set commands.verify_loop",
            TerminalSignal.STALLED,
        ),
        (
            "held",
            f"{MERGE_VERIFY_UNCONTAINED}: #1444 refusal",
            TerminalSignal.STALLED,
        ),
        (
            "held",
            f"{MERGE_QUEUE_LOCK_UNUSABLE}: /tmp/merge-queue.lock: no locks available",
            TerminalSignal.STALLED,
        ),
        (
            "held",
            f"{MERGE_VERIFY_CHECKOUT_REFUSED}: {CHECKOUT_DIRTY}",
            TerminalSignal.STALLED,
        ),
        (
            "held",
            f"{MERGE_VERIFY_BASE_UNMERGEABLE}: cannot fetch main",
            TerminalSignal.STALLED,
        ),
        ("held", f"{MERGE_VERIFY_TIMEOUT}: never finished", TerminalSignal.STALLED),
        ("held", f"{MERGE_VERIFY_UNRUNNABLE}: no shell", TerminalSignal.STALLED),
        ("held", MERGE_VERIFY_HEAD_UNREADABLE, TerminalSignal.STALLED),
        ("held", MERGE_VERIFY_NO_BRANCH, TerminalSignal.STALLED),
    ],
)
def test_a_by_design_queue_wait_never_ends_the_run_with_a_repair_code(
    action: str, reason: str, signal: TerminalSignal
) -> None:
    """Every queue refusal used to compose to STALLED, and `kew epic run` exits 1
    on STALLED — so a busy repo-wide lock (the whole point of the queue) and a
    scratch tree a live reviewer holds killed the run an operator left driving.
    Contention is progress in another process; only a pipeline an operator must
    repair may reach STALLED."""
    assert _queue_signal(MergeVerdict("101", action, reason)) is signal


def test_a_verify_lease_another_verify_holds_reads_as_progress_not_a_stall() -> None:
    """#3190: the local verify lease is machine-wide contention, like the queue
    lock — a held verdict on it keeps the tick PROGRESSING with no hold row,
    while a verify that could not run at all still reads STALLED."""
    busy = (
        f"{_mq.MERGE_VERIFY_LEASE_BUSY}: a local merge verify is already running "
        "(pid 4242, refs/heads/nightly, since 2026-10-26T12:00:00+00:00)"
    )
    assert (
        _queue_signal(MergeVerdict("101", "held", busy)) is TerminalSignal.PROGRESSING
    )
    unrunnable = f"{MERGE_VERIFY_UNRUNNABLE}: a commit kew cannot bundle"
    assert _queue_signal(MergeVerdict("101", "held", unrunnable)) is (
        TerminalSignal.STALLED
    )


def test_a_standing_failure_is_repeated_without_re_verifying_the_same_head(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the head moves nothing has changed, so the refusal must not re-run
    the gate — or record a second round — on every tick."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    verified: list[str] = []
    real = CommandCondition.check_admitted_directory

    def counting(self: CommandCondition, worktree: Path, admission: Any) -> Any:
        verified.append(str(worktree))
        return real(self, worktree, admission)

    monkeypatch.setattr(CommandCondition, "check_admitted_directory", counting)
    first = _verdicts(
        ["101", "102"],
        config=config,
        store=store,
        reader=reader,
        run=run,
        work=work,
        blocking_review=False,
    )
    assert first[1].action == "skip"

    again = _verdicts(
        ["102"],
        config=config,
        store=store,
        reader=reader,
        run=run,
        work=work,
        blocking_review=False,
    )

    assert again[0].action == "skip"
    assert again[0].reason.startswith(MERGE_VERIFY_FAILED)
    assert store.review_round_count("102") == 2  # the READY round plus one refusal
    assert len(verified) == 2  # sibling 101 and sibling 102, once each
    assert not [c for c in calls if c[3] == "502"]


def test_the_verify_command_never_runs_on_the_host_without_the_containment_opt_in(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1444: the scratch clone holds agent-authored code and the verify command
    executes over it ON THE HOST — exactly what ``CommandCondition.check()``
    refuses for an agent-written checkout without
    ``[sandbox] dangerously_skip_containment``. ``check_directory`` is the
    post-admission runner, so calling it directly must not be a way around that
    refusal: ``[epic] merge_queue = true`` alone is no authority to execute agent
    code on the host.
    """
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.sandbox.enabled = True
    config.sandbox.dangerously_skip_containment = False
    ran: list[Path] = []
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ran.append(worktree),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_UNCONTAINED)
    assert "#1444" in verdicts[0].reason
    # Nothing executed, nothing was cloned, and no PR defect was invented.
    assert ran == []
    assert not list((Path(config.branches.worktree_base) / "review").glob("pr-*"))
    assert store.latest_review_round("101").verdict == "READY"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_the_containment_opt_in_lets_the_same_candidate_through(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal above is the admission, not a blanket refusal to verify."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.sandbox.dangerously_skip_containment = True

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"


def test_the_post_verification_head_check_never_reads_the_per_tick_cache(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pr_facts`` is served from this tick's ``prepare`` batch, so re-reading the
    head through it handed back the very head the classification used — the
    head-moved refusal could never fire in production. The check must take a
    deliberately uncached read (``fresh_pr_facts``)."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    batched = reader.pr_facts
    # The batch is frozen at tick start; only the fresh read sees the push.
    reader.fresh_pr_facts = lambda pr_number: {  # type: ignore[method-assign]
        **batched(pr_number),
        "headRefOid": "f" * 40,
    }

    verdicts = merge_pass(
        ["101"],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        run=run,
        blocking_review=True,
        gated_checks=[],
        merge_verify=_stub_verifier(),
    )

    assert (verdicts[0].action, verdicts[0].reason) == ("skip", MERGE_VERIFY_HEAD_MOVED)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_an_unreadable_post_verification_head_holds_instead_of_merging(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head kew cannot read is a head it cannot bind the merge to — never an
    "unchanged" head. Hold rather than merge on a degraded read."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    reader.fresh_pr_facts = lambda pr_number: {}  # type: ignore[method-assign]

    verdicts = merge_pass(
        ["101"],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        run=run,
        blocking_review=True,
        gated_checks=[],
        merge_verify=_stub_verifier(),
    )

    assert (verdicts[0].action, verdicts[0].reason) == (
        "held",
        MERGE_VERIFY_HEAD_UNREADABLE,
    )
    assert _is_awaiting_merge(verdicts[0]) is False
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_long_verification_keeps_this_drivers_lock_heartbeat_fresh(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verification blocks inside the tick for up to
    ``MERGE_VERIFY_TIMEOUT_SECONDS`` (1800s) while the epic driver lock's
    heartbeat was only refreshed when the whole tick returned. Six times over
    ``HEARTBEAT_STALE_SECONDS`` of silence marks a perfectly healthy driver stale,
    and a stale heartbeat is stolen automatically (#970) — putting a second driver
    of the same epic on the same base, the very race the queue lock refuses."""
    assert MERGE_VERIFY_TIMEOUT_SECONDS > HEARTBEAT_STALE_SECONDS
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    monkeypatch.setattr(_mq, "MERGE_VERIFY_HEARTBEAT_SECONDS", 0.05)
    config.commands.verify_loop = "sleep 0.5"
    driver_lock = tmp_path / "epic-driver-e.lock"

    with single_driver_lock(driver_lock, holder=EPIC_REF) as got:
        assert got
        acquired = lock_holder(driver_lock)
        assert acquired is not None and acquired.heartbeat is not None
        merge_pass(
            ["101"],
            repo=REPO,
            base=BASE,
            epic_ref=EPIC_REF,
            reader=reader,
            approvers=["alice"],
            store=store,
            run=run,
            blocking_review=True,
            gated_checks=[],
            merge_verify=merge_queue_verifier(
                config,
                store,
                epic_ref=EPIC_REF,
                repo=REPO,
                base=BASE,
                repo_root=work,
                keepalive=_lock_keepalive(driver_lock, EPIC_REF),
            ),
        )
        beaten = lock_holder(driver_lock)

    assert beaten is not None and beaten.heartbeat is not None
    # Refreshed DURING the verification, before the tick that owns it ended.
    assert beaten.heartbeat > acquired.heartbeat
    assert beaten.pid == acquired.pid


def test_the_heartbeat_beat_stops_with_the_verification_it_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No beat may outlive the work it vouches for: a thread still refreshing
    after the verification returned would vouch for a driver doing nothing."""
    monkeypatch.setattr(_mq, "MERGE_VERIFY_HEARTBEAT_SECONDS", 0.05)
    beats: list[float] = []
    with keep_driver_lock_alive(lambda: beats.append(time.monotonic())):
        _wait(0.05 * 6)
    assert beats, "the keepalive never beat during the body"
    after = len(beats)
    _wait(0.05 * 4)
    assert len(beats) == after


def test_no_keepalive_is_a_plain_passthrough() -> None:
    """A caller that holds no reclaimable lock needs no thread at all."""
    before = threading.active_count()
    with keep_driver_lock_alive(None) as entered:
        assert entered is None
        assert threading.active_count() == before


def test_advance_hands_the_merge_queue_its_own_lock_heartbeat(
    driver_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring for the beat above, end to end. Without it the fix is inert:
    the queue would beat nothing and a long verification would still let the
    driver's own lock go stale under it."""
    captured: list[Any] = []
    real_run_tick = _tick._run_tick

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("lock_keepalive"))
        return real_run_tick(*args, **kwargs)

    monkeypatch.setattr(_tick, "_run_tick", spy)
    driver_env.advance()

    (beat,) = captured
    assert callable(beat)
    # The beat is bound to the lock `advance` held, and refreshes it in place.
    before = json.loads(driver_env.lock_path.read_text())
    beat()
    after = json.loads(driver_env.lock_path.read_text())
    assert after["heartbeat"] > before["heartbeat"]
    assert after["pid"] == before["pid"]
    assert after["held_since"] == before["held_since"]


def test_the_queue_verifier_carries_the_ticks_keepalive(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remaining link: the tick's keepalive reaches the queue's verifier."""
    captured: dict[str, Any] = {}

    def fake_verifier(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _stub_verifier()

    monkeypatch.setattr(_tick, "merge_queue_verifier", fake_verifier)
    config = _config(tmp_path)

    def beat() -> None:
        return None

    wired = _tick._merge_verifier(
        config, store, epic_ref=EPIC_REF, repo=REPO, base=BASE, keepalive=beat
    )

    assert wired is not None
    assert captured["keepalive"] is beat
    # And the opt-in still gates the whole thing.
    config.epic.merge_queue = False
    assert (
        _tick._merge_verifier(
            config, store, epic_ref=EPIC_REF, repo=REPO, base=BASE, keepalive=beat
        )
        is None
    )


class _NoLinearClient:
    """The gate re-reads a GitHub PR: no Linear query may be issued."""

    def execute(self, query: str, variables: Any = None) -> Any:  # pragma: no cover
        raise AssertionError("the head re-read must not query Linear")


class _NoRunStore:
    def list_by_item(self, item_id: str) -> list[Any]:  # pragma: no cover
        raise AssertionError("the head re-read must not touch the run store")


def _linear_reader(head: str) -> tuple[LinearFactsReader, list[list[str]]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"number": 42, "headRefOid": head}), ""
        )

    reader = LinearFactsReader(_NoLinearClient(), _NoRunStore(), REPO, run=fake_run)
    return reader, calls


_MERGED = MergeVerdict("SFD-7", "merged", "merged")


def _linear_gate(reader: LinearFactsReader, head_oid: str) -> Any:
    return _verified_merge(
        "SFD-7",
        item_id="SFD-7",
        pr_number=42,
        pr={"headRefName": "agent/issue-sfd-7"},
        head_oid=head_oid,
        reader=reader,  # type: ignore[arg-type]
        merge_verify=_stub_verifier(),
        merge=lambda: _MERGED,
    )


def test_a_linear_epic_can_re_read_its_head_after_verification() -> None:
    """LinearFactsReader is a duck-typed stand-in for GitHubFactsReader, NOT a
    subclass: a driver-consumed read that lands on one and not the other is an
    uncaught AttributeError that aborts the tick right after every green
    verification. The post-verification head re-read (#2148) must resolve on a
    Linear epic too, and it must reach `gh pr view`, not this tick's batch."""
    verified_head = "a" * 40
    reader, calls = _linear_reader(verified_head)

    assert _linear_gate(reader, verified_head) is _MERGED  # merge allowed
    assert [cmd[:4] for cmd in calls] == [["gh", "pr", "view", "42"]]


def test_a_linear_epic_refuses_a_head_that_moved_during_verification() -> None:
    """Same gate, other branch: the Linear delegation reports the live head, so
    the head-moved refusal fires for Linear nodes exactly as for GitHub ones."""
    reader, calls = _linear_reader("b" * 40)

    verdict = _linear_gate(reader, "a" * 40)

    assert (verdict.action, verdict.reason) == ("skip", MERGE_VERIFY_HEAD_MOVED)
    assert [cmd[:4] for cmd in calls] == [["gh", "pr", "view", "42"]]


# ---------------------------------------------------------------------------
# #2184: contained combined-tree verify, gate-definition check, base binding.
# ---------------------------------------------------------------------------


def _gate_editing_repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin whose one branch edits the gate script itself."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "scripts").mkdir()
    (work / "scripts" / "verify.sh").write_text("#!/bin/sh\nsh gate.sh\n")
    (work / "gate.sh").write_text(GATE)
    (work / "api.txt").write_text("required: none\n")
    (work / "fixtures").mkdir()
    (work / "fixtures" / "one.txt").write_text("{}\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    # The PR rewrites the gate the queue would run to prove the PR.
    (work / "scripts" / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    head = _commit(work, "always-green the gate")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


def _scanner_config_editing_repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin whose one branch edits only the SCANNERS' own config.

    No gate script, no build backend — just the secrets gate's allowlist and
    the docs gate's exclude list, alongside a credential-shaped file the
    widened allowlist now covers. This is the omitted-tool-config class (#2192):
    the PR rewrites the rules it is about to be scanned by.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "scripts").mkdir()
    (work / "scripts" / "verify.sh").write_text("#!/bin/sh\nsh gate.sh\n")
    (work / "gate.sh").write_text(GATE)
    (work / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    (work / ".gitleaksignore").write_text("")
    (work / "mkdocs.yml").write_text("site_name: t\nexclude_docs: |\n  internal/\n")
    (work / "api.txt").write_text("required: none\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    (work / ".gitleaksignore").write_text("deploy/creds.env:generic-api-key:1\n")
    (work / ".gitleaks.toml").write_text("[extend]\nuseDefault = false\n")
    (work / "mkdocs.yml").write_text("site_name: t\nexclude_docs: |\n")
    (work / "deploy").mkdir()
    (work / "deploy" / "creds.env").write_text("API_KEY=" + "z" * 32 + "\n")
    head = _commit(work, "relax the scanners")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


#: A lint gate whose rule set comes from whichever config file the tool finds
#: FIRST — the shape of every Python linter's discovery order. A `.ruff.toml`
#: sitting in the tree outranks the `pyproject.toml` beside it, so the branch
#: that ADDS one is the branch that chooses the rules it is checked by.
DISCOVERED_CONFIG_GATE = """#!/bin/sh
if [ -f .ruff.toml ]; then conf=.ruff.toml; else conf=pyproject.toml; fi
if grep -q 'select = \\[\\]' "$conf"; then exit 0; fi
if grep -l FORBIDDEN ./*.py >/dev/null 2>&1; then
  echo "lint: FORBIDDEN token"
  exit 1
fi
exit 0
"""


def _added_tool_config_repo(tmp_path: Path, *, with_config: bool) -> tuple[Path, str]:
    """A bare origin whose branch adds a lint violation, optionally with the
    conventional tool config that turns the lint gate off (#2192).

    Nothing tracked is EDITED: the config the gate obeys does not exist on the
    base at all, which is the whole point — a scan of the tree says nothing
    about a file a PR is free to introduce.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gate.sh").write_text(DISCOVERED_CONFIG_GATE)
    (work / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["ALL"]\n')
    (work / "app.py").write_text("ok = 1\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    (work / "bad.py").write_text("FORBIDDEN = 1\n")
    if with_config:
        (work / ".ruff.toml").write_text("[lint]\nselect = []\n")
    head = _commit(work, "add code the lint gate rejects")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


#: A SAST gate whose RULES are pinned by the runner and whose TARGETS are not
#: — the shape of `uvx semgrep scan --config .semgrep/pinned.yml src/`. The
#: ruleset cannot be rewritten by the branch, which is exactly why the hole
#: went unnoticed: the pattern list covered `.semgrep/` and read as if the gate
#: were handled, while `.semgrepignore` is obeyed whatever `--config` said and
#: decides what the scanner is allowed to look at at all (#2192).
SCANNER_IGNORE_GATE = """#!/bin/sh
status=0
for f in ./*.py; do
  if [ -f .semgrepignore ] && grep -qxF "${f#./}" .semgrepignore; then
    continue
  fi
  if grep -q FORBIDDEN "$f"; then
    echo "sast: FORBIDDEN token in $f"
    status=1
  fi
done
exit "$status"
"""


def _added_ignore_file_repo(tmp_path: Path, *, with_ignore: bool) -> tuple[Path, str]:
    """A bare origin whose branch adds a finding, optionally with the scanner
    ignore file that hides it from the scan (#2192).

    Same shape as ``_added_tool_config_repo`` and a different mechanism: the
    ruleset stays exactly as the base pinned it, and the branch still chooses
    what is scanned by it. Nothing tracked is edited, so every tree-reading
    oracle is silent — only the pattern list can hold this.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gate.sh").write_text(SCANNER_IGNORE_GATE)
    (work / ".semgrep").mkdir()
    (work / ".semgrep" / "pinned.yml").write_text("rules: [FORBIDDEN]\n")
    (work / "app.py").write_text("ok = 1\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    (work / "bad.py").write_text("FORBIDDEN = 1\n")
    if with_ignore:
        (work / ".semgrepignore").write_text("bad.py\n")
    head = _commit(work, "add code the sast gate rejects")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


def _one_node(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    work: Path,
    head: str,
) -> tuple[DispatchConfig, _FakeReader, Any, list[list[str]]]:
    monkeypatch.chdir(work)
    config = _config(tmp_path)
    reader = _FakeReader(
        {"101": {"pr": 501, "branch": "agent/issue-101", "head": head}}
    )
    _ready(store, "101", head)
    calls, run = _make_run(work, reader)
    return config, reader, run, calls


def test_a_pr_that_edits_the_gate_definition_is_held_before_any_command_runs(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2184 proof (a). The verify command is operator-owned, but what it
    EXECUTES is not: `scripts/verify.sh` lives in the tree under test, so a PR
    that rewrites it decides what proves it. The queue intersects the PR's own
    diff with `[epic] merge_gate_paths` BEFORE running anything, HOLDs the node
    naming the paths, and leaves the release to a human."""
    work, head = _gate_editing_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    ran: list[Path] = []
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ran.append(worktree),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    # The head rides in the reason: it is what the human's release is scoped to.
    assert verdicts[0].reason == merge_gate_hold_reason(head, "scripts/verify.sh")
    assert merge_gate_reviewed_head(verdicts[0].reason) == head
    # Nothing ran, nothing merged, and no PR defect was invented.
    assert ran == []
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert store.latest_review_round("101").verdict == "READY"
    # The hold is persisted and takes the node out of the frontier until a
    # human runs `kew epic unhold` — no later pass can clear it.
    held = [h for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"]
    assert [h.reason for h in held] == [verdicts[0].reason]
    assert holds_node_out_of_frontier(held[0].reason)


def _submodule_bump_repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin whose one branch moves only a submodule pointer under a
    gate directory, which the tree's ``.gitmodules`` marks ``ignore = all``."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "api.txt").write_text("required: none\n")
    _commit(work, "base")
    add_ignored_submodule(work, "1" * 40)
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    add_ignored_submodule(work, "2" * 40)
    head = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


def test_a_pr_that_moves_a_gate_path_submodule_its_gitmodules_ignores_is_held(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ignore = all`` drops a moved submodule pointer from a plain
    ``git diff``; the queue's gate-path read must still see it."""
    work, head = _submodule_bump_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    ran: list[Path] = []
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ran.append(worktree),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason == merge_gate_hold_reason(head, IGNORED_SUBMODULE)
    assert ran == []
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_pr_that_edits_only_the_scanners_own_config_is_held(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2192. A gate definition is not only a gate SCRIPT: the secrets gate is
    its `.gitleaks.toml` / `.gitleaksignore` and the docs gate is `mkdocs.yml`'s
    `exclude_docs`. A PR that allowlists away its own credential and then passes
    the scan it rewrote is the same class as a PR that always-greens
    `verify.sh`, so the queue must hold it before running anything."""
    work, head = _scanner_config_editing_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    ran: list[Path] = []
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ran.append(worktree),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason == merge_gate_hold_reason(
        head, ".gitleaks.toml, .gitleaksignore, mkdocs.yml"
    )
    assert ran == []
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_a_pr_that_adds_the_config_its_gate_discovers_is_held(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2192, the widest form. A gate definition does not have to EXIST on the
    base: `ruff` reads a `.ruff.toml` in preference to `pyproject.toml`, so a
    branch that ADDS one carrying `select = []` writes the rules it is then
    checked by. Nothing tracked is edited, so every oracle that scans the tree
    is silent — only the pattern list can hold this, and it must."""
    work, head = _added_tool_config_repo(tmp_path, with_config=True)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    ran: list[Path] = []
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ran.append(worktree),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason == merge_gate_hold_reason(head, ".ruff.toml")
    assert ran == []
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_the_same_pr_without_the_added_config_is_caught_red_by_the_gate(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that makes the test above a proof and not a coincidence
    (#2192): the identical violation, with the `.ruff.toml` left out, IS caught
    — so the added config is exactly what flipped the gate, and before the
    patterns covered it the queue attested `result: pass` on this branch."""
    work, head = _added_tool_config_repo(tmp_path, with_config=False)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "skip"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_FAILED)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert store.latest_review_round("101").verdict == "NEEDS_CHANGES"


def test_a_pr_that_adds_the_ignore_file_its_scanner_obeys_is_held(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2192, the form that survived pinning the ruleset. A scanner reads two
    conventional files, and covering only the rules covers half the gate:
    `.semgrepignore` is obeyed whatever `--config` pinned, so a branch that
    adds one line to it is scanned by the base's own rules and inspected for
    nothing. `**/.semgrep/` matched the pin and made the SAST gate look
    handled while this went through unheld and attested `result: pass`."""
    work, head = _added_ignore_file_repo(tmp_path, with_ignore=True)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    ran: list[Path] = []
    monkeypatch.setattr(
        CommandCondition,
        "check_admitted_directory",
        lambda self, worktree, admission: ran.append(worktree),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason == merge_gate_hold_reason(head, ".semgrepignore")
    assert ran == []
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_the_same_pr_without_the_added_ignore_file_is_caught_red(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that makes the test above a proof and not a coincidence
    (#2192): the identical finding, with `.semgrepignore` left out, IS caught
    by the same unmodified ruleset — so the added ignore file is exactly what
    silenced the scan, and before the patterns covered it the queue attested
    `result: pass` on this branch."""
    work, head = _added_ignore_file_repo(tmp_path, with_ignore=False)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "skip"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_FAILED)
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert store.latest_review_round("101").verdict == "NEEDS_CHANGES"


def _unhold(store: Any, item_id: str) -> Any:
    """Exactly what `kew epic unhold` does to release a hold (cli/epic.py)."""
    return store.remove_epic_hold(
        EPIC_REF,
        item_id,
        receipt_key=merge_gate_ack_key(EPIC_REF, item_id),
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )


def test_the_gate_hold_is_released_by_kew_epic_unhold_and_the_node_then_merges(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented recovery path has to actually recover the node (#2192).

    The hold is DERIVED from the PR's diff, so deleting the `epic_holds` row
    releases nothing on its own: the next merge pass intersects the same
    unchanged diff with the same patterns and re-derives the identical hold,
    and `kew epic unhold` is a loop the node never merges out of. The release
    records the head the human read, and this pass honors it."""
    work, head = _gate_editing_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert first[0].action == "held"

    released = _unhold(store, "101")
    assert released is not None

    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert second[0].action == "merged"
    assert [c[:4] for c in calls if c[:3] == ["gh", "pr", "merge"]] == [
        ["gh", "pr", "merge", "501"]
    ]
    assert not [h for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"]


def test_a_push_after_the_release_is_held_again_for_a_fresh_read(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt says "a human read THIS diff", never "this node is exempt".

    A commit pushed after the release is a gate edit nobody has read, so the
    node is held again — under the new head, which is the one the next release
    would cover."""
    work, head = _gate_editing_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    assert (
        _verdicts(
            ["101"], config=config, store=store, reader=reader, run=run, work=work
        )[0].action
        == "held"
    )
    assert _unhold(store, "101") is not None

    # The agent pushes one more commit to the same gate script.
    _git("checkout", "-q", "agent/issue-101", cwd=work)
    (work / "scripts" / "verify.sh").write_text("#!/bin/sh\nexit 0  # again\n")
    pushed = _commit(work, "re-edit the gate")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    reader.nodes["101"]["head"] = pushed
    _ready(store, "101", pushed)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason == merge_gate_hold_reason(pushed, "scripts/verify.sh")
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


@pytest.mark.parametrize(
    ("reason", "head"),
    [
        (f"merge_gate_touched@{'a' * 40}: scripts/verify.sh", "a" * 40),
        (f"merge_gate_touched@{'a' * 40}: a.sh, b.sh (+3 more)", "a" * 40),
        # A row written before the head was carried releases nothing: an
        # unreadable receipt holds the node for a human, which is the safe read.
        ("merge_gate_touched: scripts/verify.sh", None),
        ("merge_gate_touched", None),
        ("merge_gate_touched@: scripts/verify.sh", None),
        ("ci_pending", None),
        (None, None),
    ],
)
def test_the_reviewed_head_is_read_back_out_of_the_hold_reason(
    reason: str | None, head: str | None
) -> None:
    """The reason IS the receipt, so the head has to survive the round trip —
    and anything else has to read as "no head", never as a release."""
    assert merge_gate_reviewed_head(reason) == head
    if head is not None:
        assert holds_node_out_of_frontier(reason)


def test_the_gate_check_does_not_hold_a_pr_that_touches_no_gate_definition(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check must not be a blanket stop: an ordinary PR still merges, and
    the paths that trigger it are exactly the configured ones."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"
    assert not [h for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"]


def test_an_empty_gate_path_list_is_the_operators_way_to_turn_the_check_off(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured, not hard-coded: an operator who clears the list gets no
    check (and the queue says so by merging the very PR it would have held)."""
    work, head = _gate_editing_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    config.epic.merge_gate_paths = []

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"


@pytest.mark.parametrize(
    ("path", "held"),
    [
        ("scripts/verify.sh", True),
        ("kew/scripts/check_memory_channel.py", True),
        ("www/scripts/run-gates.mjs", True),
        ("pyproject.toml", True),
        ("kew/pyproject.toml", True),
        ("kew/tests/conftest.py", True),
        # The real locations in this repo, every one of which a root-anchored
        # spelling missed (#2192): a PR rewriting the pinned semgrep rules, or
        # adding itself to the xenon exclude list, or unpinning the gate tools
        # in a lockfile, was verified by a gate it authored.
        ("kew/.semgrep/pinned.yml", True),
        (".semgrep/pinned.yml", True),
        ("kew/.xenon-excludes", True),
        (".xenon-excludes", True),
        ("uv.lock", True),
        ("kew/uv.lock", True),
        # Every gate's OWN configuration, not just the ones a Python project
        # notices first (#2192): a PR that allowlists away its own leaked
        # credential, or that lets a page reach the published site, was
        # otherwise verified by the secrets/docs gate it had just rewritten.
        (".gitleaks.toml", True),
        (".gitleaksignore", True),
        ("mkdocs.yml", True),
        ("frontend/tsconfig.json", True),
        ("www/vite.config.ts", True),
        ("frontend/vitest.config.ts", True),
        # A config the tool finds BY CONVENTION, which is the class that has to
        # be covered before the file exists (#2192): each of these outranks the
        # `pyproject.toml` the same tool would otherwise read, so a PR that ADDS
        # one — `.ruff.toml` with `select = []` — turns the gate off and is then
        # verified green by the gate it wrote.
        (".ruff.toml", True),
        ("ruff.toml", True),
        ("kew/.ruff.toml", True),
        ("pytest.ini", True),
        ("tox.ini", True),
        ("setup.cfg", True),
        ("kew/setup.py", True),
        ("mypy.ini", True),
        (".coveragerc", True),
        (".importlinter", True),
        (".bandit", True),
        ("uv.toml", True),
        (".python-version", True),
        ("frontend/.npmrc", True),
        ("frontend/.nvmrc", True),
        (".dockerignore", True),
        # A scanner's ignore list is the other half of its gate definition
        # (#2192): the runner pins the RULES with `--config`, and these pick
        # the TARGETS, so a branch that adds one is scanned for nothing while
        # `**/.semgrep/` makes the SAST gate look covered.
        (".semgrepignore", True),
        ("kew/.semgrepignore", True),
        (".trivyignore", True),
        ("trivy.yaml", True),
        ("docker/Dockerfile", True),
        ("kew/src/kew/_docker/docker/agent.Dockerfile", True),
        ("frontend/package-lock.json", True),
        ("www/package.json", True),
        ("create-kew/package.json", True),
        (".github/workflows/ci.yml", True),
        ("kew.toml", True),
        # The demo world is where the demo browser checks read their expected
        # figures (#3141), and the leak scan's allowlist clears its findings:
        # a PR that rewrote either would be checked against what it wrote.
        ("frontend/demo/world/manifest.json", True),
        ("frontend/demo/world/generate.mjs", True),
        ("frontend/demo/world/timings.json", True),
        ("frontend/demo/leak-scan.allow.json", True),
        ("frontend/demo/leak-scan.mjs", True),
        ("frontend/demo/leak-scan.deny.json", True),
        # The demo checks tell the real build history from the samples by
        # this file's repository (#3207): a PR that renamed it would choose
        # which assertions its own build is checked by.
        ("frontend/demo/public-repo.json", True),
        ("frontend/demo/fixture-bundle.ts", False),
        ("kew/src/kew/graph/driver/merge_queue.py", False),
        ("docs/configuration.md", False),
        ("scripts.md", False),
        ("notdocker/Dockerfile", False),
        ("kew/src/kew/scripts.py", False),
    ],
)
def test_the_shipped_gate_path_defaults_name_what_defines_a_gate(
    path: str, held: bool
) -> None:
    """The default list is the contract: everything that decides what the gate
    RUNS is in it, and ordinary source is not (a check that held every PR would
    be turned off on the first day)."""
    patterns = DispatchConfig().epic.merge_gate_paths
    assert bool(_mq.touched_gate_paths([path], patterns)) is held


def test_the_verify_child_runs_contained_and_sees_no_host_environment(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2184 proof (b). The command executes the PR's own files, so it runs in
    the approved image over a mounted checkout — never as a host child of the
    driver that holds the merge credentials. The launch carries no network and
    none of the host's environment: no HOME, no token."""
    from kew import sandbox
    from kew.loops import verification

    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    # The ordinary contained posture: a mapped image the runtime can resolve,
    # and NO `dangerously_skip_containment`.
    config.sandbox.dangerously_skip_containment = False
    config.sandbox.images = {"claude-code": "approved:tag"}
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: "/bin/docker")
    monkeypatch.setattr(
        verification, "require_image_id", lambda *a, **k: "sha256:" + "d" * 64
    )
    monkeypatch.setenv("HOME", "/host/home/operator")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_hosttokenvalue")
    launched: list[list[str]] = []

    def launch(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        launched.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", launch)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"  # a contained green still merges
    assert len(launched) == 1
    argv = launched[0]
    assert argv[:2] == ["docker", "run"]
    assert "sha256:" + "d" * 64 in argv
    # `--entrypoint /bin/sh`, then the image, then the command as its args.
    assert argv[argv.index("--entrypoint") + 1] == "/bin/sh"
    assert argv[-2:] == ["-c", "sh gate.sh"]
    assert argv[argv.index("--network") + 1] == "none"
    # Every environment assignment the child gets, and nothing from the host.
    passed = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-e"]
    # HOME is reassigned to the in-container tmpfs; every other assignment the
    # launch carries is an explicit blanking (the proxy names).
    assert "HOME=/tmp" in passed  # noqa: S108 — in-container tmpfs, not a host path
    assert not [
        value
        for value in passed
        if value.split("=", 1)[1] and not value.startswith("HOME=")
    ]
    joined = " ".join(argv)
    assert "/host/home/operator" not in joined
    assert "ghp_hosttokenvalue" not in joined


def test_a_container_that_could_not_launch_is_not_a_pr_defect(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime that refuses the launch proves nothing about the PR. It holds
    the merge as an operator problem — never a NEEDS_CHANGES round an agent
    would be dispatched to 'fix'."""
    from kew import sandbox
    from kew.loops import verification

    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.sandbox.dangerously_skip_containment = False
    config.sandbox.images = {"claude-code": "approved:tag"}
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: "/bin/docker")
    monkeypatch.setattr(
        verification, "require_image_id", lambda *a, **k: "sha256:" + "d" * 64
    )
    monkeypatch.setattr(
        verification,
        "run_container_with_cpu_cap_retry",
        lambda argv, **kwargs: (_ for _ in ()).throw(OSError("no such runtime")),
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_UNRUNNABLE)
    assert store.latest_review_round("101").verdict == "READY"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def test_gates_that_cannot_run_contained_are_named_to_the_child_and_recorded(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate the contained run cannot execute is not a gate that passed. The
    declared names reach the command (so one that can skip them does) and ride
    on the attestation with the reason, so the green says what it did not
    prove."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.epic.merge_queue_uncontained_gates = ["docker build", "npm ci"]
    seen: list[str] = []
    real = CommandCondition.check_admitted_directory

    def capture(self: CommandCondition, worktree: Path, admission: Any) -> Any:
        seen.append(self._command)
        return real(self, worktree, admission)

    monkeypatch.setattr(CommandCondition, "check_admitted_directory", capture)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"
    assert seen == [
        f"{_mq.UNCONTAINED_GATES_ENV}='docker build,npm ci'; "
        f"export {_mq.UNCONTAINED_GATES_ENV}; sh gate.sh"
    ]
    attested = [
        event.payload or {} for event in store.list_audit_events(event="merge_verify")
    ]
    assert attested[-1]["skipped_gates"] == ["docker build", "npm ci"]
    assert attested[-1]["skipped_reason"] == _mq.UNCONTAINED_GATES_REASON
    # The command that is REPORTED stays the operator's own.
    assert attested[-1]["command"] == "sh gate.sh"


def test_a_base_that_moves_during_the_verification_refuses_the_merge(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2184 proof (c). The queue lock serializes kew's merges, but nothing
    stops a human (or another bot) merging into main while the gates run. The
    tree was built on the OID recorded before the command; re-read after it, a
    base that moved means the green describes a tree that will never exist."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    mover = tmp_path / "move-the-base.sh"
    mover.write_text(
        "#!/bin/sh\n"
        "set -e\n"
        f"cd {work}\n"
        f"git checkout -q {BASE}\n"
        "echo external >> unrelated.txt\n"
        "git add -A\n"
        "git commit -qm 'someone else merged'\n"
        f"git push -q origin {BASE}\n"
        "git checkout -q --detach\n"
    )
    # Exit 0: the gates PASSED. Only the base binding withholds this merge.
    config.commands.verify_loop = f"sh {mover}"

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert (verdicts[0].action, verdicts[0].reason) == (
        "skip",
        _mq.MERGE_VERIFY_BASE_MOVED,
    )
    assert _is_awaiting_merge(verdicts[0]) is True  # not a stall: re-verify next tick
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert store.latest_review_round("101").verdict == "READY"


def test_a_red_tree_whose_base_moved_is_not_blamed_on_the_pr(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2192. The base binding is checked before the red is attributed, not
    after. A NEEDS_CHANGES round is not a withheld merge the next tick retries:
    it stands against the head until a remediation and a re-review clear it. So
    recording one for a tree that went red against a base something outside kew
    has since moved costs a whole review cycle — and blocks the queue from
    re-verifying the NEW combined tree, which is the only thing that could say
    whether the PR is red at all."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    mover = tmp_path / "move-the-base-then-fail.sh"
    mover.write_text(
        "#!/bin/sh\n"
        "set -e\n"
        f"cd {work}\n"
        f"git checkout -q {BASE}\n"
        "echo external >> unrelated.txt\n"
        "git add -A\n"
        "git commit -qm 'someone else merged'\n"
        f"git push -q origin {BASE}\n"
        "git checkout -q --detach\n"
        "echo 'gate: red against the base that just moved'\n"
        "exit 1\n"
    )
    config.commands.verify_loop = f"sh {mover}"

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert (verdicts[0].action, verdicts[0].reason) == (
        "skip",
        _mq.MERGE_VERIFY_BASE_MOVED,
    )
    assert _is_awaiting_merge(verdicts[0]) is True  # re-verify next tick
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    # The standing round is what would have cost the cycle: the head is still
    # READY, so the next tick verifies the new combined tree instead of waiting
    # on a remediation for a defect nothing has shown the PR to have.
    assert store.latest_review_round("101").verdict == "READY"
    assert [
        event.payload or {} for event in store.list_audit_events(event="merge_verify")
    ] == []


def test_a_red_attestation_carries_the_same_facts_as_a_green_one(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2192. A red is a decision about one specific combined tree. Without the
    base OID nothing can say afterwards WHICH main this head was red against,
    without the posture nothing can say what it ran under, and the gates the
    containment could not run are exactly as unproven here as on a pass — which
    is what `[epic] merge_queue_uncontained_gates` and the docs promise for
    EVERY merge-verify attestation, not only the green ones."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.epic.merge_queue_uncontained_gates = ["docker build"]
    base_sha = _git("rev-parse", f"origin/{BASE}", cwd=work)
    config.commands.verify_loop = "sh -c 'echo gate: red; exit 1'"

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "skip"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_FAILED)
    attested = [
        event.payload or {} for event in store.list_audit_events(event="merge_verify")
    ]
    assert attested[-1]["result"] == "fail"
    assert attested[-1]["base_sha"] == base_sha
    assert attested[-1]["posture"] == HOST_VERIFICATION_POSTURE
    assert attested[-1]["skipped_gates"] == ["docker build"]
    assert attested[-1]["skipped_reason"] == _mq.UNCONTAINED_GATES_REASON


def test_the_verified_base_oid_is_recorded_with_the_green(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attestation binds the green to a base, not just to a branch name:
    without the OID nothing can say afterwards WHICH main was verified."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    base_sha = _git("rev-parse", f"origin/{BASE}", cwd=work)

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"
    attested = [
        event.payload or {} for event in store.list_audit_events(event="merge_verify")
    ]
    assert attested[-1]["base_sha"] == base_sha
    assert attested[-1]["result"] == "pass"


def test_the_queue_refuses_to_infer_its_command_from_commands_ci(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2184: `commands.ci` is usually a narrower gate (on kew itself, pytest
    alone). Falling back to it merged combined trees nothing had linted while
    the node's evidence said the queue had verified them."""
    config, reader, run, work, calls = _two_siblings(tmp_path, store, monkeypatch)
    config.commands.verify_loop = ""
    config.commands.ci = "sh gate.sh"

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "held"
    assert verdicts[0].reason.startswith(MERGE_VERIFY_UNCONFIGURED)
    assert "verify_loop" in verdicts[0].reason
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]


def _assert_removing_repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin whose one branch deletes an assert from a test (#2486)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gate.sh").write_text(GATE)
    (work / "api.txt").write_text("required: none\n")
    (work / "fixtures").mkdir()
    (work / "fixtures" / "one.txt").write_text("{}\n")
    (work / "tests").mkdir()
    test_file = work / "tests" / "test_widget.py"
    test_file.write_text("def test_widget():\n    assert 1 == 1\n    assert True\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    test_file.write_text("def test_widget():\n    assert True\n")
    head = _commit(work, "drop an assert")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


def _ready_with_sidecar(
    tmp_path: Path, store: Any, item_id: str, head: str, verdict: dict[str, Any]
) -> None:
    sidecar = tmp_path / f"review-sidecar-{item_id}.json"
    sidecar.write_text(json.dumps(verdict))
    store.record_review_round(
        EPIC_REF,
        item_id,
        head_sha=head,
        verdict="READY",
        review_mode="full",
        sidecar_path=str(sidecar),
        sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    )


def test_a_ready_verdict_that_leaves_a_test_flag_unanswered_is_held(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2486: a PR whose diff removes an assert, reviewed READY with no **Test
    changes** section, is held like a gate-file edit — and released the same
    way, by `kew epic unhold` on that head."""
    work, head = _assert_removing_repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert first[0].action == "held"
    assert first[0].reason == merge_gate_hold_reason(
        head, "unanswered test-integrity flags asserts_removed"
    )
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    held = [h for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"]
    assert [h.reason for h in held] == [first[0].reason]

    assert _unhold(store, "101") is not None
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"


@pytest.mark.parametrize(
    ("answer", "action"), [("ACCEPTED", "merged"), ("REJECTED", "held")]
)
def test_a_ready_verdict_that_accepts_each_test_flag_is_not_held(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    action: str,
) -> None:
    work, head = _assert_removing_repo(tmp_path)
    config, reader, run, _calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    change = {"flag": "asserts_removed", "answer": answer, "reason": "issue: AC2"}
    _ready_with_sidecar(
        tmp_path, store, "101", head, {"verdict": "READY", "test_changes": [change]}
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == action


def _console_and_site_editing_repo(tmp_path: Path) -> tuple[Path, str]:
    """:func:`_console_editing_repo`, whose branch edits kew.dev (``www/``) too:
    a page with a browser check of its own that no ``kew verify`` runs."""
    return _console_editing_repo(tmp_path, site=True)


def _console_editing_repo(tmp_path: Path, *, site: bool = False) -> tuple[Path, str]:
    """A bare origin whose one branch edits the console (``frontend/``), the
    diff a ``kew verify`` runs its browser ui-check for."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gate.sh").write_text(GATE)
    (work / "api.txt").write_text("required: none\n")
    (work / "frontend" / "src").mkdir(parents=True)
    (work / "frontend" / "src" / "chat.ts").write_text("export const chat = 1;\n")
    _commit(work, "base")
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "origin", BASE, cwd=work)

    _git("checkout", "-qb", "agent/issue-101", cwd=work)
    (work / "frontend" / "src" / "chat.ts").write_text("export const chat = 2;\n")
    if site:
        (work / "www").mkdir()
        (work / "www" / "index.html").write_text("<h1>kew</h1>\n")
    head = _commit(work, "change the chat")
    _git("push", "-q", "origin", "agent/issue-101", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work, head


def _sibling_101(tmp_path: Path) -> tuple[Path, str]:
    """The first green sibling: a diff that touches nothing ui-check covers."""
    work, heads = _sibling_repo(tmp_path)
    return work, heads["101"]


def _unchecked_node(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    kinds: list[str],
    *,
    repo: Any = _sibling_101,
    accepted: bool = False,
) -> tuple[DispatchConfig, _FakeReader, Any, list[list[str]], Path, str]:
    """One node from *repo*, reviewed READY — or an accepted NEEDS_CHANGES —
    with *kinds* in ``not_checked`` (#3421)."""
    work, head = repo(tmp_path)
    config, reader, run, calls = _one_node(
        tmp_path, store, monkeypatch, work=work, head=head
    )
    record_reviewed(
        store,
        tmp_path,
        epic_ref=EPIC_REF,
        item_id="101",
        head=head,
        kinds=kinds,
        accepted=accepted,
        repo=REPO,
    )
    return config, reader, run, calls, work, head


def test_a_ready_naming_only_the_full_suite_merges_through_the_queue(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue's gate is the verify command over main + head: it runs the
    full suite the review sandbox could not, so no hold is added (#3421)."""
    config, reader, run, calls, work, _head = _unchecked_node(
        tmp_path, store, monkeypatch, ["full_suite"]
    )

    verdicts = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert verdicts[0].action == "merged"
    assert [h for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"] == []


def test_a_ready_naming_docker_is_held_until_unhold_releases_its_head(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, reader, run, calls, work, head = _unchecked_node(
        tmp_path, store, monkeypatch, ["docker", "full_suite"]
    )
    ran: list[Path] = []
    real = CommandCondition.check_admitted_directory

    def counting(self: CommandCondition, worktree: Path, admission: Any) -> Any:
        ran.append(worktree)
        return real(self, worktree, admission)

    monkeypatch.setattr(CommandCondition, "check_admitted_directory", counting)

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    reason = merge_gate_hold_reason(head, "review sandbox could not run: docker")
    assert (first[0].action, first[0].reason) == ("held", reason)
    assert ran == [] and not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    held = [h.reason for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"]
    assert held == [reason]

    assert _unhold(store, "101") is not None
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"
    assert len(ran) == 1


def test_a_ready_naming_a_browser_check_merges_after_a_browser_checked_verify(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A console change: a ``kew verify`` of it runs the browser ui-check."""
    config, reader, run, calls, work, head = _unchecked_node(
        tmp_path, store, monkeypatch, ["browser"], repo=_console_editing_repo
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    assert (first[0].action, first[0].reason) == (
        "skip",
        MERGE_VERIFY_BROWSER_UNCHECKED,
    )
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert [h for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"] == []

    # The verify host's browser-checked verify of main merged into the head.
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("policy", "verify-host"),
        payload={
            "ref_sha": "d" * 40,
            "ref_parents": [head, "e" * 40],
            "ui_check": True,
            "ui_check_modules": ["chat"],
            "outcome": "passed",
        },
    )
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"


def _merges(calls: list[list[str]]) -> list[list[str]]:
    return [c[:4] for c in calls if c[:3] == ["gh", "pr", "merge"]]


def _node_holds(store: Any) -> list[str]:
    return [h.reason for h in store.list_epic_holds(EPIC_REF) if h.item_id == "101"]


def test_a_browser_check_no_verify_of_the_diff_runs_is_held_until_unhold(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff touches nothing under ``frontend/``, so no ``kew verify`` of it
    ever runs ui-check: a wait for one would never end. A person runs the
    browser check, and the head's receipt releases it."""
    config, reader, run, calls, work, head = _unchecked_node(
        tmp_path, store, monkeypatch, ["browser"]
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    reason = merge_gate_hold_reason(head, "review sandbox could not run: browser")
    assert (first[0].action, first[0].reason) == ("held", reason)
    assert _merges(calls) == [] and _node_holds(store) == [reason]

    assert _unhold(store, "101") is not None
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"
    assert _merges(calls) == [["gh", "pr", "merge", "501"]]


def test_one_hold_names_the_gate_edit_and_the_unrun_check_and_one_unhold_releases(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person who releases the head sees every risk the release answers:
    the gate script it edits AND the Docker check its review could not run."""
    config, reader, run, calls, work, head = _unchecked_node(
        tmp_path, store, monkeypatch, ["docker"], repo=_gate_editing_repo
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    reason = merge_gate_hold_reason(
        head, "scripts/verify.sh; review sandbox could not run: docker"
    )
    assert (first[0].action, first[0].reason) == ("held", reason)
    assert merge_gate_reviewed_head(reason) == head
    assert _merges(calls) == [] and _node_holds(store) == [reason]

    assert _unhold(store, "101") is not None
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"
    assert _node_holds(store) == []


def test_an_accepted_needs_changes_naming_docker_is_held_until_unhold(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop rule's accepted residuals (#2112) merge like a READY, so what
    that round's reviewer could not run is held the same."""
    config, reader, run, calls, work, head = _unchecked_node(
        tmp_path, store, monkeypatch, ["docker"], accepted=True
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    reason = merge_gate_hold_reason(head, "review sandbox could not run: docker")
    assert (first[0].action, first[0].reason) == ("held", reason)
    assert _merges(calls) == [] and _node_holds(store) == [reason]

    assert _unhold(store, "101") is not None
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"


def test_the_console_s_ui_check_does_not_answer_a_www_browser_check(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR edits the console AND kew.dev. A passing ui-check verify of it is
    on record, but no ``kew verify`` runs www's ``npm run browser-check``: the
    browser check the reviewer could not run is a person's."""
    config, reader, run, calls, work, head = _unchecked_node(
        tmp_path, store, monkeypatch, ["browser"], repo=_console_and_site_editing_repo
    )
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("policy", "verify-host"),
        payload={
            "ref_sha": "d" * 40,
            "ref_parents": [head, "e" * 40],
            "ui_check": True,
            "ui_check_modules": None,
            "outcome": "passed",
        },
    )

    first = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )

    reason = merge_gate_hold_reason(head, "review sandbox could not run: browser")
    assert (first[0].action, first[0].reason) == ("held", reason)
    assert _merges(calls) == [] and _node_holds(store) == [reason]

    assert _unhold(store, "101") is not None
    second = _verdicts(
        ["101"], config=config, store=store, reader=reader, run=run, work=work
    )
    assert second[0].action == "merged"
