"""The project's merge queue, verified with ``kew verify`` (#2814).

While GitHub Actions cannot run, a merge needs some other proof that the tree
it produces is green. ``kew merge queue add <PR> --head <sha>`` puts a head
kew's review found READY in line; the line is a FIFO in the ledger
(:mod:`kew.run_store_merge_queue`) that every session on the host shares, so
two sessions no longer starve each other and a reboot loses nothing.

**One pass** (:meth:`QueueRunner.run_once`, from ``kew drive`` or ``kew merge
queue run``) holds the repo-wide merge-queue lock that ``kew epic merge`` and
the drive's standing rule take, and a second lock beside the ledger file, so
one verification and one merge run at a time whoever started them and
whichever config they read. It takes the first ``[merge_queue] train``
entries, and:

1. reads each PR fresh: a merged PR is recorded merged; a closed one, one that
   no longer targets the configured repository and base branch, and a head
   that changed while queued are dropped with the reason;
2. fetches main once — the base every verify of the pass, bisection included,
   is built on and is told about — and builds the candidate exactly as GitHub
   will land it: each head squashed, in queue order, onto the squash before
   it. It runs ``kew verify --host <[verify] host>`` on that commit with the
   persisted **Behavior changes** declarations and every entry's current
   acceptance contract. A declaring entry verifies alone, and a declaration
   edit after enqueue drops it. A gate-definition edit, an unanswered
   test-integrity flag or a check the review sandbox could not run is held
   before verify, under one reason, until a human-release receipt names that
   exact head; a browser check a verify can still record waits for one. Either
   way the pass skips that PR and goes on to the next queued one. Each head is
   bound to the one node kew reviewed it
   READY as (:func:`review_refusal`), and a head READY for two nodes is not
   queued;
3. before each merge, re-reads main and the PR: main must be the base the train
   was verified on, then each squash this pass made, and the PR still open at
   its head on the configured base. It also requires the same acceptance
   contract versions that passed. A change requeues the remaining train and
   raises attention. A head-bound merge rejected after a push drops the old
   entry with requeue guidance. Main moving before the first merge sends entries
   back once. A second move drops an entry. Main moving later stops the train;
4. claims the entry in the ledger, squash-merges it through the REST API bound
   to its queued head, requires GitHub's answer to say it merged, compares the
   tree main then holds with the tree verified for that prefix, and records
   the landed state before comparing its tree. A mismatch becomes terminal and
   holds the queue until main moves and verifies green. A PR that landed the
   verified tree gets a confirmed post-merge mutation-report queue write
   (:class:`~kew.drive_merge.ReportQueue`) before it becomes terminal. A later
   runner resumes this sequence after a crash. Terminal verdict comments stay
   pending in the ledger until an idempotent delivery succeeds.
   :meth:`QueueRunner.run_reports` runs owed reports after the pass.

A train whose verify fails is bisected over its prefixes — the first half, then
the rest — down to the first PR whose prefix fails. That PR is failed with the
reason, and the train without it is verified again. A PR that does not merge
cleanly onto main and the PRs before it is that PR. ``train = 1`` is a plain
one-at-a-time queue.

A train verifies only its final tree, so one that stops part-way — a refused
merge, a moved main, a crash that left a claim — leaves a main nobody
verified. The next pass verifies main itself first; while main is red it
merges nothing and raises attention, once per main.

TRUST BOUNDARY: an entry is an operator's word, typed at a shell on the host,
for a head kew's own review found READY — the authority ``kew epic merge``
acts on. Nothing an agent writes is read: not a label, not a comment; the
heads come from GitHub, the review verdict from the ledger and the verify
verdict from ``kew verify``.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import re
import socket
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from kew import merge_queue_attention as attention
from kew.audit import Actor, emit
from kew.config import DispatchConfig
from kew.drive_merge import PendingReport, ReportQueue, probe_verify_host
from kew.epic_holds import MERGE_GATE_TOUCHED_HOLD_PREFIX
from kew.graph.driver.facts import RunFn
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_BROWSER_UNCHECKED,
    MERGE_VERIFY_LEASE_BUSY,
    UNCHECKED_FULL_SUITE,
    acceptance_contract_change,
    acceptance_contract_versions,
    merge_gate_refusal,
    merge_queue_lock,
    merge_queue_lock_path,
    review_unchecked_kinds,
)
from kew.merge_queue_attention import ATTENTION_EVENT, NOTICE_EVENT
from kew.merge_verdict import judge_head
from kew.merge_verify import (
    VerifyLeaseRefused,
    VerifyRefused,
    declared_behavior_changes,
    run_mutation_report,
    run_verify,
)
from kew.notifier import NotificationEvent, build_notifier
from kew.review_residuals import acceptance_on_record
from kew.run_store import RunStore
from kew.run_store_acceptance import AcceptanceContractRow
from kew.run_store_merge_queue import (
    DROPPED,
    FAILED,
    LANDED,
    LANDED_MISMATCH,
    MERGED,
    MERGING,
    REMOVED,
    MergeQueueEntry,
)
from kew.subprocess_utils import run as _default_run

_logger = logging.getLogger(__name__)

ADDED_EVENT = "merge_queue_added"
REMOVED_EVENT = "merge_queue_removed"
SETTLED_EVENT = "merge_queue_settled"

BODY_CHANGED = "body changed after enqueue; re-enqueue after review"

#: How many times main may move under an entry's verification before the
#: entry is dropped rather than sent back to its place.
MAX_REQUEUES = 1

#: The review verdict that admits a head on its own; a NEEDS_CHANGES round
#: the stop rule accepted on both of its records admits it too.
_READY = "READY"

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
_BASE_REF = "refs/kew/merge-queue/base"
_IDENTITY = ("-c", "user.name=kew", "-c", "user.email=kew@invalid.local")


class QueueError(RuntimeError):
    """An operator request the queue refuses, or a forge that would not answer."""


class _MergeRefused(QueueError):
    """GitHub authoritatively refused a merge request."""


class _MergeOutcomeUnknown(QueueError):
    """A merge request may have landed despite its unusable response."""


class _GhApiFailure(QueueError):
    """A failed ``gh api`` process, including any returned HTTP status."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        match = _HTTP_STATUS_RE.search(detail)
        self.status = None if match is None else int(match.group(1))

    @property
    def definitively_refused(self) -> bool:
        """Whether GitHub returned a client refusal that cannot land later."""
        return (
            self.status is not None and 400 <= self.status < 500 and self.status != 408
        )


def default_session() -> str:
    """Who is enqueuing: ``KEW_SESSION`` when set, else user and shell pid."""
    named = os.environ.get("KEW_SESSION", "").strip()
    if named:
        return named
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"{user}:{os.getppid()}"


def _pass_owner() -> str:
    """The claim owner a pass writes on the entries it merges."""
    return f"{socket.gethostname()}:{os.getpid()}"


def checked_head(head: str) -> str:
    """*head* as a full lowercase commit id, or a refusal."""
    sha = head.strip().lower()
    if not _SHA_RE.match(sha):
        raise QueueError(f"--head must be a full 40-hex commit id, not {head!r}")
    return sha


# ── the forge ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PullState:
    """One PR as GitHub reads it now."""

    number: int
    state: str
    merged: bool
    head: str
    title: str
    body: str
    #: The branch and repository the PR merges into.
    base_ref: str = ""
    base_repo: str = ""
    draft: bool = False
    #: The commit a merged PR landed as; empty while it is unmerged.
    merge_commit: str = ""


def _merge_landed_sha(pull: PullState, raw: str) -> str:
    """The full commit from an authoritative successful merge response."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _MergeOutcomeUnknown(
            f"GitHub's answer to merging PR #{pull.number} is not JSON: {raw!r}"
        ) from exc
    landed = str(data.get("sha") or "").lower() if isinstance(data, dict) else ""
    if isinstance(data, dict) and data.get("merged") is False:
        raise _MergeRefused(f"GitHub did not say PR #{pull.number} merged: {raw!r}")
    if not isinstance(data, dict) or data.get("merged") is not True:
        raise _MergeOutcomeUnknown(
            f"GitHub's merge outcome for PR #{pull.number} is unknown: {raw!r}"
        )
    if not _SHA_RE.match(landed):
        raise _MergeOutcomeUnknown(
            f"GitHub said PR #{pull.number} merged but named no commit: {raw!r}"
        )
    return landed


def _pull_state(pr: int, data: Any) -> PullState:
    return PullState(
        number=pr,
        state=str(data["state"]),
        merged=bool(data.get("merged")),
        head=str(data["head"]["sha"]).lower(),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        base_ref=str(data["base"]["ref"]),
        base_repo=str(data["base"]["repo"]["full_name"]),
        draft=bool(data.get("draft")),
        merge_commit=str(data.get("merge_commit_sha") or "").lower(),
    )


@dataclass
class GhForge:
    """GitHub's REST API, through ``gh api``."""

    repo: str
    run: RunFn = _default_run

    def _api(self, *args: str) -> str:
        result = self.run(["gh", "api", *args], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or "no stderr"
            raise _GhApiFailure(f"gh api {args[-1]}: {detail}", detail)
        return str(result.stdout or "")

    def pull(self, pr: int) -> PullState:
        raw = self._api(f"repos/{self.repo}/pulls/{pr}")
        try:
            return _pull_state(pr, json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            raise QueueError(
                f"PR #{pr} does not read as a pull request: {exc}"
            ) from exc

    def _merge_api(self, pull: PullState, head: str) -> str:
        """Send one merge PUT, classifying process and HTTP failures."""
        try:
            return self._api(
                "-X",
                "PUT",
                "-f",
                "merge_method=squash",
                "-f",
                f"sha={head}",
                "-f",
                f"commit_title={pull.title} (#{pull.number})",
                "-f",
                f"commit_message={pull.body}",
                f"repos/{self.repo}/pulls/{pull.number}/merge",
            )
        except _GhApiFailure as exc:
            error = _MergeRefused if exc.definitively_refused else _MergeOutcomeUnknown
            raise error(str(exc)) from exc

    def merge(self, pull: PullState, head: str) -> str:
        """Squash-merge *pull* only while its head is still *head*.

        The commit main landed as, read from GitHub's answer — which must say
        ``merged: true`` with a commit id, or the merge is not believed.
        """
        return _merge_landed_sha(pull, self._merge_api(pull, head))

    def comment(self, pr: int, text: str) -> None:
        self._api("-f", f"body={text}", f"repos/{self.repo}/issues/{pr}/comments")

    def ensure_comment(self, pr: int, text: str, marker: str) -> None:
        """Post *text* once, including after an ambiguous earlier failure."""
        path = f"repos/{self.repo}/issues/{pr}/comments"
        raw = self._api("--paginate", "--slurp", path)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise QueueError(f"comments for PR #{pr} are not JSON: {raw!r}") from exc
        if not isinstance(data, list):
            raise QueueError(f"comments for PR #{pr} are not a list")
        comments: list[dict[str, Any]] = []
        for page in data:
            records = page if isinstance(page, list) else [page]
            if not all(isinstance(comment, dict) for comment in records):
                raise QueueError(f"comments for PR #{pr} contain an invalid record")
            comments.extend(records)
        if any(marker in str(comment.get("body") or "") for comment in comments):
            return
        self.comment(pr, f"{text}\n\n{marker}")


def binding_refusal(pull: PullState, repo: str, base: str) -> str | None:
    """Why *pull* does not merge into *repo*'s *base*, or None when it does."""
    if pull.base_repo.lower() != repo.lower():
        return f"PR #{pull.number} merges into {pull.base_repo}, not {repo}"
    if pull.base_ref != base:
        return f"PR #{pull.number} targets {pull.base_ref}, not {base}"
    return None


@dataclass(frozen=True)
class ReviewBinding:
    """The node a queued head was reviewed READY as."""

    epic_ref: str
    item_id: str


def ready_reviews(store: RunStore, head: str) -> list[ReviewBinding]:
    """Every node whose latest kew review round found *head* READY.

    READY, or a NEEDS_CHANGES round whose residuals the stop rule accepted on
    both of its records — the verdicts ``kew epic merge`` acts on. A round for
    another head, or a later round that is not READY, admits nothing.
    """
    bound = []
    for item_id in store.review_items_at_head(head):
        latest = store.latest_review_round(item_id)
        if latest is None or latest.head_sha != head:
            continue
        if latest.verdict == _READY or acceptance_on_record(store, item_id, latest):
            bound.append(ReviewBinding(latest.epic_ref, item_id))
    return bound


def review_refusal(store: RunStore, head: str) -> str | None:
    """Why *head* is not bound to exactly one node kew reviewed READY, or None.

    The node is whose acceptance contract the verify runs, so a head READY
    for two nodes is refused rather than one of their contracts picked.
    """
    bound = ready_reviews(store, head)
    refused = _bound_refusal(head, bound)
    if refused is not None:
        return refused
    # The one merge verdict rule (#3196). The bound round passed while not
    # READY only as an accepted NEEDS_CHANGES.
    latest = store.latest_review_round(bound[0].item_id)
    check = judge_head(
        store, head, accepted=latest is not None and latest.verdict != _READY
    )
    return None if check.passed else check.detail


def _bound_refusal(head: str, bound: Sequence[ReviewBinding]) -> str | None:
    """Why *head*, READY for the nodes *bound*, is not bound to exactly one."""
    if not bound:
        return (
            f"no kew review found {head[:12]} READY — only a head whose latest "
            "review round is READY (or READY with accepted residuals) is queued"
        )
    if len(bound) > 1:
        nodes = ", ".join(f"{b.epic_ref}/{b.item_id}" for b in bound)
        return (
            f"kew's review found {head[:12]} READY for {len(bound)} nodes "
            f"({nodes}), so whose acceptance contract binds it is ambiguous"
        )
    return None


def _edited_drop(entry: MergeQueueEntry) -> bool:
    """Whether the queue dropped *entry* for a declaration edited after enqueue."""
    return entry.state == DROPPED and (entry.reason or "").endswith(BODY_CHANGED)


def _reviewed_since(store: RunStore, head: str, since: datetime) -> bool:
    """Whether a review round recorded after *since* found *head* READY."""
    for binding in ready_reviews(store, head):
        latest = store.latest_review_round(binding.item_id)
        if latest is not None and latest.recorded_at > since:
            return True
    return False


# ── the candidate tree ──────────────────────────────────────────────────────


class _GitError(RuntimeError):
    pass


class _Conflict(_GitError):
    """The PR at ``index`` does not merge onto main and the PRs before it."""

    state = FAILED

    def __init__(self, index: int, detail: str) -> None:
        super().__init__(detail)
        self.index = index


def _held_until(reason: str) -> str:
    """What lifts a pre-verify refusal, said the way the operator acts on it."""
    if reason == MERGE_VERIFY_BROWSER_UNCHECKED:
        return (
            "verification waits for a passing `kew verify` of this head that "
            "ran the browser check."
        )
    return "verification is held until a human releases this head."


class _HeadMoved(_Conflict):
    """The PR at ``index`` was pushed after the pass read it."""

    state = DROPPED


class _Withdrawn(_Conflict):
    """The PR at ``index`` lost its admission after the pass read it."""

    state = DROPPED


@dataclass(frozen=True)
class Candidate:
    """A tree to verify: main, the commit built on it, the tree after each PR."""

    base_sha: str
    sha: str
    trees: tuple[str, ...] = ()


@dataclass
class GitCandidate:
    """main with the queued heads squashed onto it, in order, in a fresh worktree."""

    repo_root: Path
    base: str = "main"
    run: RunFn = _default_run

    def _git(self, *args: str, cwd: Path | None = None, strip: bool = True) -> str:
        where = self.repo_root if cwd is None else cwd
        result = self.run(["git", "-C", str(where), *args], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise _GitError(f"git {args[0]} exited {result.returncode}: {detail}")
        out = str(result.stdout or "")
        return out.strip() if strip else out

    def base_now(self) -> str:
        """main as origin has it now."""
        out = self._git("ls-remote", "origin", f"refs/heads/{self.base}")
        sha = out.split()[0].lower() if out else ""
        if not _SHA_RE.match(sha):
            raise _GitError(f"origin has no {self.base} branch to read")
        return sha

    def fetch_base(self) -> str:
        """Fetch main; the commit every candidate of a pass is built on."""
        self._git(
            "fetch", "--no-tags", "origin", f"+refs/heads/{self.base}:{_BASE_REF}"
        )
        return self._git("rev-parse", f"{_BASE_REF}^{{commit}}").lower()

    def tree_of(self, sha: str) -> str:
        """The tree of *sha*, a commit main holds now (fetched first)."""
        self.fetch_base()
        return self._git("rev-parse", f"{sha}^{{tree}}").lower()

    def changed_paths(self, base_sha: str, head: str) -> tuple[str, ...]:
        """One queued head's net paths against the pass's fetched base."""
        names = self._git(
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{base_sha}...{head}",
            strip=False,
        )
        return tuple(sorted(path for path in names.split("\0") if path))

    def _fetch_heads(self, entries: Sequence[MergeQueueEntry]) -> None:
        refspecs = [
            f"+refs/pull/{e.pr}/head:refs/kew/merge-queue/pr/{e.pr}" for e in entries
        ]
        self._git("fetch", "--no-tags", "origin", *refspecs)
        for index, entry in enumerate(entries):
            ref = f"refs/kew/merge-queue/pr/{entry.pr}^{{commit}}"
            if self._git("rev-parse", ref).lower() != entry.head:
                raise _HeadMoved(
                    index,
                    f"the head changed while queued: PR #{entry.pr} moved "
                    f"off {entry.head[:12]}",
                )

    def _squash(self, tree: Path, index: int, entry: MergeQueueEntry) -> str:
        """Squash *entry* onto *tree*'s HEAD as GitHub does; the new tree id."""
        try:
            self._git(*_IDENTITY, "merge", "--squash", entry.head, cwd=tree)
        except _GitError as exc:
            raise _Conflict(index, str(exc)) from exc
        self._git(
            *_IDENTITY,
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            f"kew merge queue: squash of #{entry.pr}",
            cwd=tree,
        )
        return self._git("rev-parse", "HEAD^{tree}", cwd=tree).lower()

    @contextlib.contextmanager
    def build(
        self, entries: Sequence[MergeQueueEntry], base_sha: str
    ) -> Iterator[Candidate]:
        """The candidate for *entries* on *base_sha*; main itself when empty.

        Each head is squashed onto the squash before it, so the commit is the
        tree squash-merging the PRs in order lands, not the one a chain of
        merge commits would make: a PR stacked on an earlier one that deletes
        that one's file still three-way-merges against main, as on GitHub.
        """
        if not entries:
            yield Candidate(base_sha, base_sha)
            return
        self._fetch_heads(entries)
        with tempfile.TemporaryDirectory(prefix="kew-merge-queue-") as scratch:
            tree = Path(scratch) / "tree"
            self._git("worktree", "add", "--detach", str(tree), base_sha)
            try:
                trees = tuple(
                    self._squash(tree, index, entry)
                    for index, entry in enumerate(entries)
                )
                head = self._git("rev-parse", "HEAD", cwd=tree).lower()
                yield Candidate(base_sha, head, trees)
            finally:
                with contextlib.suppress(_GitError):
                    self._git("worktree", "remove", "--force", str(tree))


# ── one verification ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Verdict:
    #: ``passed``, ``failed`` (the culprit is unknown: bisect), ``culprit``
    #: (the PR at ``index`` is the one), ``held`` (no verdict; retry later) or
    #: ``skip`` (the PR at ``index`` waits for a person or a browser check;
    #: the pass goes on without it).
    kind: str
    reason: str = ""
    base_sha: str = ""
    host: str = ""
    index: int = -1
    #: What the culprit becomes: ``failed``, or ``dropped`` for a moved head.
    state: str = FAILED
    #: A passing verdict's tree after each PR of the train, in order.
    trees: tuple[str, ...] = ()
    #: The acceptance-contract row ids and versions that this verdict ran.
    contracts: tuple[tuple[int, int], ...] = ()


def combined_body(entries: Sequence[MergeQueueEntry]) -> str | None:
    """A one-entry train's declarations, exactly as persisted at enqueue.

    A multi-entry train never receives declarations. The batch selector keeps
    a declaring entry alone, and this function fails closed if that invariant
    is broken by granting no behavior exception.
    """
    if len(entries) != 1 or not entries[0].behavior_changes:
        return None
    lines = (
        f"- {node} — declared before queue admission"
        for node in entries[0].behavior_changes
    )
    return "**Behavior changes**\n" + "\n".join(lines)


def _names(entries: Sequence[MergeQueueEntry]) -> str:
    return " + ".join(f"#{entry.pr}" for entry in entries) or "nothing"


# ── the runner ──────────────────────────────────────────────────────────────


@dataclass
class QueuePass:
    """What one pass did."""

    merged: list[MergeQueueEntry] = field(default_factory=list)
    settled: list[tuple[MergeQueueEntry, str, str]] = field(default_factory=list)
    requeued: list[MergeQueueEntry] = field(default_factory=list)
    verifies: int = 0
    held: str | None = None


@dataclass
class QueueRunner:
    """Take the head of the queue, verify it, and merge what passes."""

    config: DispatchConfig
    store: RunStore
    repo: str
    repo_root: Path
    base: str = "main"
    run: RunFn = _default_run
    verify: Callable[..., Any] = run_verify
    #: The post-merge mutation report job (:func:`run_mutation_report`).
    report: Callable[..., Any] = run_mutation_report
    probe: Callable[[DispatchConfig], str | None] = probe_verify_host
    say: Callable[[str], None] = _logger.info
    #: Where attention goes; ``[alerting]`` when None.
    notify: Callable[[NotificationEvent], None] | None = None
    forge: GhForge | None = None
    candidate: GitCandidate | None = None
    owner: str = field(default_factory=_pass_owner)
    #: The mains this runner knows green: verified alone, or landed as a
    #: verified tree. Bisection verifies any other main before blaming a PR.
    _green: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.forge is None:
            self.forge = GhForge(self.repo, self.run)
        if self.candidate is None:
            self.candidate = GitCandidate(self.repo_root, self.base, self.run)

    @property
    def _forge(self) -> GhForge:
        assert self.forge is not None  # nosec B101 — set in __post_init__
        return self.forge

    @property
    def _tree(self) -> GitCandidate:
        assert self.candidate is not None  # nosec B101 — set in __post_init__
        return self.candidate

    @contextlib.contextmanager
    def _locks(self) -> Iterator[str | None]:
        """Hold the repo-wide lock and the ledger's; None, or why one is held.

        The repo-wide lock (from config) is the one ``kew epic merge`` and the
        standing rule take. The ledger's is derived from the ``dispatch.db``
        file, so two configs that share one ledger cannot both take its rows.
        """
        paths = (
            merge_queue_lock_path(self.config),
            self.store.merge_queue_ledger_lock_path(),
        )
        with contextlib.ExitStack() as stack:
            for path in paths:
                if path is None:
                    continue
                lease = stack.enter_context(merge_queue_lock(path))
                if not lease.acquired:
                    yield lease.reason or "the merge queue is held"
                    return
            yield None

    def run_once(self) -> QueuePass:
        """One pass over the head of the queue, under the queue's locks.

        A held main is work even with nothing queued: an operator merge or a
        landed mismatch can leave one behind, and only a pass verifies it.
        """
        done = QueuePass()
        if not self._has_work() and not self.store.list_pending_merge_reports(
            self.repo
        ):
            return done
        with self._locks() as held:
            if held is not None:
                done.held = held
                return done
            self._deliver_pending_verdicts()
            if self._has_work():
                self._pass(done)
        return done

    def _has_work(self) -> bool:
        """Whether a pass has entries to take or a held main to verify."""
        return bool(self.store.list_merge_queue(self.repo)) or (
            self.store.merge_queue_suspect(self.repo) is not None
        )

    @property
    def _reports(self) -> ReportQueue:
        return ReportQueue(
            self.config,
            self.store,
            repo=self.repo,
            repo_root=self.repo_root,
            report=self.report,
            run=self.run,
            say=self.say,
        )

    def run_reports(self) -> None:
        """Run the owed post-merge mutation reports, after the pass's merges.

        The verify host is probed first: a report that reaches a host that is
        gone is lost, while one that waits stays owed for a later pass.
        """
        reports = self._reports
        if reports.owed() and self.probe(self.config) is None:
            reports.run_owed()

    def _pass(self, done: QueuePass) -> None:
        if not self._resolve_claims(done):
            return
        try:
            base_sha = self._tree.fetch_base()
        except _GitError as exc:
            self._note_held(done, f"cannot fetch {self.base}: {exc}")
            return
        if not self._main_green(base_sha, done):
            return
        self._settle_queue(base_sha, done)

    def _settle_queue(self, base_sha: str, done: QueuePass) -> None:
        """Settle the first train of the queue on *base_sha*.

        A PR held for a person or waiting on a browser check is stepped past,
        never a wall in front of the PRs queued behind it (#3421): the pass
        takes the next train without it. A pass that skipped every PR it could
        take is held, by the first skip's reason.
        """
        skipped: list[tuple[MergeQueueEntry, str]] = []
        while True:
            passed = {entry.id for entry, _ in skipped}
            entries = [
                e for e in self.store.list_merge_queue(self.repo) if e.id not in passed
            ][: self.config.merge_queue.train]
            live = self._live(entries, done)
            if not live:
                break
            more = self._settle(self._train_batch(live), base_sha, done)
            if not more:
                return
            skipped += more
        if skipped and done.held is None:
            done.held = skipped[0][1]

    def _note_held(self, done: QueuePass, reason: str) -> None:
        done.held = reason
        _logger.warning("kew merge queue: pass held: %s", reason)

    # ── what an earlier pass left ───────────────────────────────────────────

    def _resolve_claims(self, done: QueuePass) -> bool:
        """Settle the claims a pass cut off mid-merge left, from GitHub's state.

        Under the queue's locks no other pass is merging, so every claim seen
        here is stale. A PR GitHub merged is recorded merged; any other goes
        back to its place and is verified again. Either way the train it was
        part of stopped part-way, so main is verified before anything else.
        """
        for entry in self.store.list_merge_queue(self.repo):
            if entry.state == LANDED:
                if not self._finish_landed(entry, done):
                    return False
                continue
            if entry.state == MERGING and not self._resolve_claim(entry, done):
                return False
        return True

    def _resolve_claim(self, entry: MergeQueueEntry, done: QueuePass) -> bool:
        """Resolve one stale claim from GitHub's current state."""
        try:
            pull = self._forge.pull(entry.pr)
        except QueueError as exc:
            self._note_held(done, str(exc))
            return False
        self.store.suspect_merge_queue_main(
            self.repo, f"a pass ({entry.owner}) stopped while merging #{entry.pr}"
        )
        if not pull.merged:
            if entry.merge_unknown:
                self._note_held(
                    done,
                    f"PR #{entry.pr}'s merge outcome is still unknown; GitHub "
                    "still reports it open, so its merge claim remains held",
                )
                return False
            self.store.release_merge_entry(entry.id)
            return True
        if pull.head != entry.head:
            self._settle_merged_outside(entry, pull, done)
            return True
        if not _SHA_RE.match(pull.merge_commit):
            self._note_held(
                done,
                f"PR #{entry.pr} is merged, but GitHub has not supplied a full "
                "merge commit SHA; keeping its merge claim for retry",
            )
            return False
        return self._resume_claimed(entry, pull, done)

    def _resume_claimed(
        self, entry: MergeQueueEntry, pull: PullState, done: QueuePass
    ) -> bool:
        """Move a stale claimed merge to landed validation."""
        reason = (
            f"merge resumed after its pass stopped; landed as {pull.merge_commit[:12]}"
        )
        if not self.store.record_landed_merge(entry.id, pull.merge_commit, reason):
            self._note_held(done, f"cannot resume landed PR #{entry.pr}")
            return False
        landed = replace(
            entry,
            state=LANDED,
            reason=reason,
            owner=None,
            landed_sha=pull.merge_commit,
        )
        return self._finish_landed(landed, done)

    def _main_green(self, base_sha: str, done: QueuePass) -> bool:
        """Whether main may be built on: verified first after a partial train."""
        suspect = self.store.merge_queue_suspect(self.repo)
        if suspect is None:
            return True
        why, red_on = suspect
        if red_on == base_sha:
            self._note_held(
                done, f"{self.base} {base_sha[:12]} is red; waiting for it to move"
            )
            return False
        verdict = self._verify([], base_sha, done)
        if verdict.kind == "passed":
            self.store.clear_merge_queue_suspect(self.repo)
            self._green.add(base_sha)
            self.say(f"kew merge queue: {self.base} {base_sha[:12]} verified green.")
            return True
        if verdict.kind == "held":
            self._note_held(done, verdict.reason)
            return False
        self._main_red(why, base_sha, verdict, done)
        return False

    def _main_red(
        self, why: str, base_sha: str, verdict: _Verdict, done: QueuePass
    ) -> None:
        """Hold the queue on a main a verify found red, and say so once."""
        # The failure is kept beside the cause: the event below is said once,
        # and the durable row is what tells a person what to repair (#2883).
        self.store.suspect_merge_queue_main(
            self.repo, why, red_on=base_sha, failure=verdict.reason
        )
        message = (
            f"kew merge queue: {self.base} {base_sha[:12]} is red after {why} — "
            f"{verdict.reason}. Nothing merges until {self.base} verifies green."
        )
        self._attention(attention.MAIN_RED, message, base_sha=base_sha)
        self._note_held(done, message)

    # ── reading the entries ─────────────────────────────────────────────────

    def _live(
        self, entries: Sequence[MergeQueueEntry], done: QueuePass
    ) -> list[MergeQueueEntry]:
        """The entries still worth verifying; the rest settled with why."""
        live = []
        for entry in entries:
            try:
                pull = self._forge.pull(entry.pr)
            except QueueError as exc:
                self._note_held(done, str(exc))
                return []
            if pull.merged:
                self._settle_merged_outside(entry, pull, done)
                continue
            changed = self._body_change_reason(entry, pull)
            if changed is not None:
                self._settle_entry(entry, DROPPED, changed, done)
                self._attention(
                    attention.DECLARATION_EDITED,
                    f"kew merge queue: PR #{entry.pr} {changed}.",
                    pr=entry.pr,
                    base_sha=entry.head,
                )
                continue
            gone = self._gone_reason(entry, pull)
            if gone is None:
                live.append(entry)
            else:
                self._settle_entry(entry, DROPPED, gone, done)
        return live

    @staticmethod
    def _train_batch(live: list[MergeQueueEntry]) -> list[MergeQueueEntry]:
        """Keep every declaring entry in a one-entry verification train."""
        for index, entry in enumerate(live):
            if not entry.behavior_changes:
                continue
            return live[:index] if index else live[:1]
        return live

    @staticmethod
    def _body_change_reason(entry: MergeQueueEntry, pull: PullState) -> str | None:
        current = tuple(sorted(declared_behavior_changes(pull.body)))
        if current == entry.behavior_changes:
            return None
        return BODY_CHANGED

    def _gone_reason(self, entry: MergeQueueEntry, pull: PullState) -> str | None:
        """Why an unmerged *pull* no longer merges as queued, or None."""
        if pull.state != "open":
            return f"PR #{entry.pr} is {pull.state}"
        if pull.draft:
            return f"PR #{entry.pr} is a draft"
        bound = binding_refusal(pull, self.repo, self.base)
        if bound is not None:
            return bound
        if pull.head != entry.head:
            return (
                f"the head changed while queued: {entry.head[:12]} -> "
                f"{pull.head[:12]}; queue the new head once it is READY"
            )
        body_change = self._body_change_reason(entry, pull)
        if body_change is not None:
            return body_change
        # Re-proved before the verify and again just before the merge: a
        # later round on this head that is not READY withdraws admission.
        refused = review_refusal(self.store, entry.head)
        if refused is not None:
            return f"no longer admitted: {refused}"
        return None

    def _settle_merged_outside(
        self, entry: MergeQueueEntry, pull: PullState, done: QueuePass
    ) -> None:
        """Record a PR GitHub already merged: merged only at the queued head.

        A merge the queue makes is bound to the queued head, so a merged head
        that differs was merged by someone else, as code no review the queue
        admitted read; the entry is dropped, never recorded merged.
        """
        if pull.head != entry.head:
            reason = (
                f"merged on GitHub outside the queue at {pull.head[:12]}, not the "
                f"queued {entry.head[:12]}; the queue merged nothing"
            )
            self._settle_entry(
                entry,
                DROPPED,
                reason,
                done,
                sha=pull.head,
                merged_sha=pull.merge_commit,
            )
            return
        reason = (
            "already merged on GitHub (outside the queue, or by a pass cut off "
            "before it recorded the merge)"
        )
        self._settle_entry(entry, MERGED, reason, done, merged_sha=pull.merge_commit)

    # ── settling a train ────────────────────────────────────────────────────

    def _settle(
        self, batch: list[MergeQueueEntry], base_sha: str, done: QueuePass
    ) -> list[tuple[MergeQueueEntry, str]]:
        """Verify *batch* on *base_sha*; merge it, or fail its culprit and retry.

        An entry the verify cannot prove without a person or a browser check is
        skipped, its refusal recorded (the hold, the attention), and the rest
        of the batch verified without it. Returns the skipped entries when
        they were the whole batch, so the pass takes the PRs queued behind
        them; empty once anything else settled the batch.
        """
        skipped: list[tuple[MergeQueueEntry, str]] = []
        failed = False
        while batch:
            verdict = self._verify(batch, base_sha, done)
            if verdict.kind == "skip":
                skipped.append((batch[verdict.index], verdict.reason))
                batch = batch[: verdict.index] + batch[verdict.index + 1 :]
                continue
            if verdict.kind == "held":
                done.held = verdict.reason
                return []
            if verdict.kind == "passed":
                self._merge_verified(batch, verdict, done)
                return []
            found = self._culprit(batch, verdict, done)
            if found is None:
                return []
            self._settle_entry(batch[found.index], found.state, found.reason, done)
            batch = batch[: found.index] + batch[found.index + 1 :]
            failed = True
        return [] if failed else skipped

    def _culprit(
        self, batch: list[MergeQueueEntry], verdict: _Verdict, done: QueuePass
    ) -> _Verdict | None:
        """The first PR whose prefix of *batch* fails (its ``index``), and why.

        Bisects over prefixes — the whole train is known to fail — so each PR
        is judged on main plus the PRs queued before it, the tree it would
        merge onto. Every probe is built on the train's base, never a main
        fetched again. None when a verify in between reached no verdict.
        """
        if verdict.kind == "culprit":
            return verdict
        low, high, reason = 1, len(batch), verdict.reason
        while low < high:
            mid = (low + high) // 2
            probe = self._verify(batch[:mid], verdict.base_sha, done)
            if probe.kind in ("held", "skip"):
                done.held = probe.reason
                return None
            if probe.kind == "culprit":
                return probe
            if probe.kind == "passed":
                low = mid + 1
            else:
                high, reason = mid, probe.reason
        if high == 1 and not self._main_passes(batch[0], verdict.base_sha, done):
            return None
        return _Verdict("culprit", reason, base_sha=verdict.base_sha, index=high - 1)

    def _main_passes(
        self, first: MergeQueueEntry, base_sha: str, done: QueuePass
    ) -> bool:
        """Whether main alone verifies, before its first queued PR is blamed.

        Main may have gone red outside the queue (a human or standing-rule
        merge, a flake); every PR would then fail in turn. A red main holds
        the queue instead, as after a partial train.
        """
        if base_sha in self._green:
            return True
        verdict = self._verify([], base_sha, done)
        if verdict.kind == "passed":
            self._green.add(base_sha)
            return True
        if verdict.kind == "held":
            done.held = verdict.reason
        else:
            self._main_red(f"#{first.pr} failed on it", base_sha, verdict, done)
        return False

    def _verify(
        self, batch: Sequence[MergeQueueEntry], base_sha: str, done: QueuePass
    ) -> _Verdict:
        """``kew verify`` of *batch* squashed onto *base_sha*, as a verdict."""
        try:
            with self._tree.build(batch, base_sha) as candidate:
                gate = self._preverify_refusal(batch, base_sha)
                if gate is not None:
                    entry, verdict = gate
                    self._attention(
                        attention.HEAD_HELD,
                        f"kew merge queue: PR #{entry.pr} {verdict.reason}; "
                        + _held_until(verdict.reason),
                        pr=entry.pr,
                        base_sha=entry.head,
                    )
                    return verdict
                done.verifies += 1
                contracts = self._contracts(batch)
                # Said only when it is so: a batch that names no full_suite
                # calls the verify exactly as one that predates the rule.
                full: dict[str, bool] = (
                    {"force_full": True} if self._names_full_suite(batch) else {}
                )
                _cand, result = self.verify(
                    self.config,
                    store=self.store,
                    ref=candidate.sha,
                    repo_root=self.repo_root,
                    pr_body=combined_body(batch),
                    base_sha=base_sha,
                    contracts=contracts,
                    require_receipt=True,
                    **full,
                )
        except (_HeadMoved, _Withdrawn) as exc:
            return _Verdict(
                "culprit", str(exc), base_sha=base_sha, index=exc.index, state=DROPPED
            )
        except _Conflict as exc:
            return _Verdict(
                "culprit",
                f"does not merge cleanly onto {self.base} {base_sha[:12]} + "
                f"earlier queued PRs ({exc})",
                base_sha=base_sha,
                index=exc.index,
            )
        except _GitError as exc:
            return _Verdict("held", f"no candidate to verify: {exc}", base_sha=base_sha)
        except VerifyLeaseRefused as exc:
            # Another local verify has the machine (#3190): the pass waits for
            # it, and the next one verifies this train.
            return _Verdict(
                "held", f"{MERGE_VERIFY_LEASE_BUSY}: {exc}", base_sha=base_sha
            )
        except VerifyRefused as exc:
            return _Verdict(
                "held", f"kew verify operational failure: {exc}", base_sha=base_sha
            )
        return self._judge(batch, result, candidate, contracts)

    def _preverify_refusal(
        self, batch: Sequence[MergeQueueEntry], base_sha: str
    ) -> tuple[MergeQueueEntry, _Verdict] | None:
        """The first entry the queue's verify cannot prove without a person or a
        browser check (:func:`merge_gate_refusal`), as a ``skip`` the pass
        steps past; ``held`` when its diff could not be read."""
        for index, entry in enumerate(batch):
            binding = self._binding(entry, index)
            refusal = merge_gate_refusal(
                self._tree.changed_paths(base_sha, entry.head),
                self.repo_root,
                self.config,
                self.store,
                epic_ref=binding.epic_ref,
                item_id=binding.item_id,
                base_sha=base_sha,
                head_oid=entry.head,
            )
            if refusal is not None:
                # A person's hold or a browser wait is this PR's alone; an
                # unreadable diff is the checkout's, and holds the pass.
                skips = refusal.action == "skip" or refusal.reason.startswith(
                    MERGE_GATE_TOUCHED_HOLD_PREFIX
                )
                return entry, _Verdict(
                    "skip" if skips else "held",
                    refusal.reason,
                    base_sha=base_sha,
                    index=index,
                )
        return None

    def _names_full_suite(self, batch: Sequence[MergeQueueEntry]) -> bool:
        """Whether a READY in *batch* names a ``full_suite`` its reviewer could
        not run (#3421). The queue waives that entry only because its verify
        runs the full suite, so this verify does, whatever ``[verify] suite``
        says: a tiered run of selected tests answers nothing about the rest."""
        return any(
            UNCHECKED_FULL_SUITE
            in review_unchecked_kinds(
                self.store, self._binding(entry, index).item_id, entry.head
            )
            for index, entry in enumerate(batch)
        )

    def _binding(self, entry: MergeQueueEntry, index: int) -> ReviewBinding:
        """The one node this queued head still has READY review authority for."""
        bound = ready_reviews(self.store, entry.head)
        refused = _bound_refusal(entry.head, bound)
        if refused is not None:
            raise _Withdrawn(index, f"no longer admitted: {refused}")
        (binding,) = bound
        return binding

    def _contracts(
        self, batch: Sequence[MergeQueueEntry]
    ) -> list[AcceptanceContractRow]:
        """Each entry's current acceptance contract, in queue order.

        Read fresh for every verify, so an amended contract is the one run.
        Admission is re-proved here too: a review round recorded while an
        earlier verify of the pass ran — the bisection's probes come later —
        can withdraw it, and such an entry is dropped (:class:`_Withdrawn`)
        rather than one of its contracts guessed. One without a contract adds
        none.
        """
        rows = []
        for index, entry in enumerate(batch):
            binding = self._binding(entry, index)
            row = self.store.current_acceptance_contract(
                binding.epic_ref, binding.item_id
            )
            if row is not None:
                rows.append(row)
        return rows

    def _judge(
        self,
        batch: Sequence[MergeQueueEntry],
        result: Any,
        candidate: Candidate,
        contracts: Sequence[AcceptanceContractRow],
    ) -> _Verdict:
        base_sha = candidate.base_sha
        what = f"{self.base} {base_sha[:12]} + {_names(batch)}"
        if type(result.receipt) is not int or result.receipt < 1:
            return _Verdict(
                "held",
                f"kew verify returned no durable receipt for {what}",
                base_sha=base_sha,
            )
        if result.outcome == "passed":
            return _Verdict(
                "passed",
                base_sha=base_sha,
                host=result.host,
                trees=candidate.trees,
                contracts=acceptance_contract_versions(contracts),
            )
        if result.outcome == "incomplete" and self.probe(self.config) is not None:
            return _Verdict(
                "held",
                f"kew verify reached no verdict on {result.host}: host gone",
                base_sha=base_sha,
            )
        code = result.exit_code
        detail = result.error or ("no exit code" if code is None else f"exit {code}")
        return _Verdict(
            "failed",
            f"kew verify {result.outcome} on {result.host} for {what} ({detail})",
            base_sha=base_sha,
        )

    # ── merging a verified train ────────────────────────────────────────────

    def _merge_verified(
        self, batch: Sequence[MergeQueueEntry], verdict: _Verdict, done: QueuePass
    ) -> None:
        """Merge a verified train in order, each onto the main it was verified on.

        A train that stops after some of it merged leaves a main no verify
        read, so the next pass verifies main first.
        """
        expected = verdict.base_sha
        for index in range(len(batch)):
            landed = self._merge_entry(batch, index, expected, verdict, done)
            if landed is None:
                if index:
                    self.store.suspect_merge_queue_main(
                        self.repo,
                        f"a train stopped after {_names(batch[:index])} merged",
                    )
                return
            expected = landed

    def _merge_entry(
        self,
        batch: Sequence[MergeQueueEntry],
        index: int,
        expected: str,
        verdict: _Verdict,
        done: QueuePass,
    ) -> str | None:
        """Merge ``batch[index]`` onto main at *expected*; the commit it landed
        as, or None when the train stops here."""
        entry = batch[index]
        pull = self._ready_to_merge(batch, index, expected, verdict, done)
        if pull is None:
            return None
        if not self.store.claim_merge_entry(
            entry.id,
            self.owner,
            expected_base=expected,
            expected_tree=verdict.trees[index],
        ):
            # Removed while it was verified: an operator's no stands, and
            # the PRs behind it were verified on top of it, so stop here.
            self.say(f"kew merge queue: PR #{entry.pr} left the queue; stopping.")
            return None
        try:
            landed = self._forge.merge(pull, entry.head)
        except QueueError as exc:
            return self._record_merge_failure(entry, exc, done)
        reason = (
            f"kew verify passed on {verdict.host} for {self.base} "
            f"{verdict.base_sha[:12]} + {_names(batch)}; landed as {landed[:12]}"
        )
        if not self.store.record_landed_merge(entry.id, landed, reason):
            self._note_held(
                done,
                f"PR #{entry.pr} merged as {landed[:12]}, but its landed state "
                "could not be recorded",
            )
            return None
        landed_entry = replace(
            entry,
            state=LANDED,
            reason=reason,
            owner=None,
            expected_base=expected,
            expected_tree=verdict.trees[index],
            landed_sha=landed,
        )
        return landed if self._finish_landed(landed_entry, done) else None

    def _finish_landed(self, entry: MergeQueueEntry, done: QueuePass) -> bool:
        """Validate and report one landed entry before making it terminal."""
        if not entry.expected_base or not entry.expected_tree or not entry.landed_sha:
            message = (
                f"kew merge queue: landed PR #{entry.pr} has incomplete validation "
                "state; the queue stopped"
            )
            self.store.suspect_merge_queue_main(
                self.repo, f"#{entry.pr} landed without validation state"
            )
            self._attention(
                attention.MAIN_UNVERIFIED,
                message,
                pr=entry.pr,
                base_sha=entry.landed_sha or "",
            )
            self._note_held(done, message)
            return False
        if not self._landed_as_verified(
            entry, entry.landed_sha, entry.expected_tree, done
        ):
            return False
        self._reports.add(
            PendingReport("", entry.pr, entry.expected_base, entry.landed_sha)
        )
        reason = entry.reason or (
            f"landed as {entry.landed_sha[:12]} and matched its verified tree"
        )
        self._settle_entry(entry, MERGED, reason, done, merged_sha=entry.landed_sha)
        self._green.add(entry.landed_sha)
        return True

    def _ready_to_merge(
        self,
        batch: Sequence[MergeQueueEntry],
        index: int,
        expected: str,
        verdict: _Verdict,
        done: QueuePass,
    ) -> PullState | None:
        """The PR re-read just before its merge, or None when it must not merge.

        main must be *expected* — the base the train was verified on, then each
        squash this pass made — and the PR still open at its queued head on the
        configured repository and base branch.
        """
        entry = batch[index]
        try:
            now = self._tree.base_now()
            pull = self._forge.pull(entry.pr)
        except (_GitError, QueueError) as exc:
            self._note_held(
                done, f"cannot re-read before merging PR #{entry.pr}: {exc}"
            )
            return None
        if now != expected:
            if index == 0:
                self._requeue(batch, expected, now, done)
            else:
                self._note_held(
                    done,
                    f"{self.base} is at {now[:12]}, not {expected[:12]} as this "
                    f"train left it; #{entry.pr} and after verify again",
                )
            return None
        if pull.merged:
            self._settle_merged_outside(entry, pull, done)
            return None
        gone = self._gone_reason(entry, pull)
        if gone is not None:
            self._settle_entry(entry, DROPPED, f"not merged: {gone}", done)
            if gone == BODY_CHANGED:
                self._attention(
                    attention.DECLARATION_EDITED,
                    f"kew merge queue: PR #{entry.pr} {gone}.",
                    pr=entry.pr,
                    base_sha=entry.head,
                )
            return None
        if not self._contracts_still_current(batch, index, verdict, done):
            return None
        return pull

    def _contracts_still_current(
        self,
        batch: Sequence[MergeQueueEntry],
        index: int,
        verdict: _Verdict,
        done: QueuePass,
    ) -> bool:
        """Requeue the unmerged train if its verified contracts changed."""
        try:
            rows = self._contracts(batch)
        except _Withdrawn as exc:
            self._record_withdrawal(batch, index, exc, done)
            return False
        current = acceptance_contract_versions(rows)
        detail = acceptance_contract_change(verdict.contracts, current)
        if detail is None:
            return True
        reason = f"acceptance contract changed while verification ran: {detail}"
        remaining = batch[index:]
        for entry in remaining:
            if self.store.requeue_merge_entry(entry.id, reason, count_main_move=False):
                done.requeued.append(entry)
                self.say(f"kew merge queue: PR #{entry.pr} re-queued: {reason}.")
        first = batch[index]
        self._attention(
            attention.CONTRACT_AMENDED,
            f"kew merge queue: PR #{first.pr} {reason}; the train must verify again.",
            pr=first.pr,
            base_sha=first.head,
        )
        return False

    def _record_withdrawal(
        self,
        batch: Sequence[MergeQueueEntry],
        index: int,
        exc: _Withdrawn,
        done: QueuePass,
    ) -> None:
        """Drop the head a review round withdrew; requeue the train after it.

        The round landed between the head's last re-read and the contracts'
        final one. That is not an amended contract: the entry has no review
        authority left, so it is dropped now rather than requeued to be dropped
        next pass, and the rest of the train, verified with it, verifies again.
        """
        lost = batch[exc.index]
        if exc.index >= index:
            self._settle_entry(lost, DROPPED, f"not merged: {exc}", done)
        reason = f"PR #{lost.pr} lost its review admission before the merge"
        for entry in batch[index:]:
            if entry is lost:
                continue
            if self.store.requeue_merge_entry(entry.id, reason, count_main_move=False):
                done.requeued.append(entry)
                self.say(f"kew merge queue: PR #{entry.pr} re-queued: {reason}.")
        self._attention(
            attention.REVIEW_WITHDRAWN,
            f"kew merge queue: PR #{lost.pr} {exc}; dropped, and the train after "
            "it must verify again.",
            pr=lost.pr,
            base_sha=lost.head,
        )

    def _record_merge_failure(
        self, entry: MergeQueueEntry, exc: QueueError, done: QueuePass
    ) -> str | None:
        """Settle a refused merge, or retain an ambiguous request's claim.

        Only an authoritative refusal can fail an unmerged entry. A transport
        failure or unusable response may hide an accepted PUT, so it stays
        claimed until GitHub reports a full landed SHA or withdrawal.
        """
        _logger.warning("kew merge queue: PR #%s not merged: %s", entry.pr, exc)
        unknown = isinstance(exc, _MergeOutcomeUnknown)
        if unknown and not self.store.record_unknown_merge(
            entry.id, f"merge outcome unknown: {exc}"
        ):
            self._note_held(
                done,
                f"PR #{entry.pr}'s merge outcome is unknown and could not be recorded",
            )
            return None
        try:
            pull = self._forge.pull(entry.pr)
        except QueueError as again:
            self._note_held(done, f"PR #{entry.pr}'s merge is unknown: {exc}; {again}")
            return None
        if pull.merged:
            if pull.head != entry.head:
                self._settle_merged_outside(entry, pull, done)
                return None
            if not _SHA_RE.match(pull.merge_commit):
                self._note_held(
                    done,
                    f"PR #{entry.pr} is merged, but GitHub has not supplied a full "
                    "merge commit SHA; keeping its merge claim for retry",
                )
                return None
            return (
                pull.merge_commit if self._resume_claimed(entry, pull, done) else None
            )
        gone = self._gone_reason(entry, pull)
        if gone is not None:
            self._settle_entry(entry, DROPPED, gone, done)
            return None
        if unknown:
            self._note_held(
                done,
                f"PR #{entry.pr}'s merge outcome is unknown after {exc}; GitHub "
                "still reports it open, so its merge claim remains held",
            )
            return None
        self._settle_entry(entry, FAILED, f"the merge was refused: {exc}", done)
        return None

    def _landed_as_verified(
        self, entry: MergeQueueEntry, landed: str, verified: str, done: QueuePass
    ) -> bool:
        """Whether main now holds the tree verified for this prefix."""
        try:
            actual = self._tree.tree_of(landed)
        except _GitError as exc:
            self._note_held(
                done,
                f"cannot read the tree landed by PR #{entry.pr}: {exc}",
            )
            return False
        if actual == verified:
            return True
        reason = (
            f"landed as {landed} with actual tree {actual}, not the verified tree "
            f"{verified}"
        )
        message = f"kew merge queue: PR #{entry.pr} {reason}; the train stopped"
        self.store.suspect_merge_queue_main(
            self.repo,
            f"#{entry.pr} landed unverified",
            mismatch_on=landed,
            failure=f"PR #{entry.pr} {reason}",
        )
        self._settle_entry(entry, LANDED_MISMATCH, reason, done, merged_sha=landed)
        self._attention(
            attention.MAIN_UNVERIFIED, message, pr=entry.pr, base_sha=landed
        )
        self._note_held(done, message)
        return False

    def _requeue(
        self, batch: Sequence[MergeQueueEntry], was: str, now: str, done: QueuePass
    ) -> None:
        reason = (
            f"{self.base} moved from {was[:12]} to {now[:12]} while the "
            "verification ran"
        )
        for entry in batch:
            if entry.requeues >= MAX_REQUEUES:
                self._settle_entry(
                    entry, DROPPED, f"{reason}, again; re-add it to retry", done
                )
            elif self.store.requeue_merge_entry(entry.id, reason, count_main_move=True):
                done.requeued.append(entry)
                self.say(f"kew merge queue: PR #{entry.pr} re-queued: {reason}.")

    # ── telling people ──────────────────────────────────────────────────────

    def _attention(
        self,
        reason: str,
        message: str,
        *,
        pr: int | None = None,
        base_sha: str = "",
    ) -> None:
        """Say, record and notify something an operator has to look at.

        *reason* is a :mod:`kew.merge_queue_attention` code; it names the event
        and the severity, so only a person's next move is raised as attention.
        """
        self.say(message)
        event = ATTENTION_EVENT if attention.person_next(reason) else NOTICE_EVENT
        emit(
            self.store,
            event,
            actor=Actor(type="policy", id="merge-queue"),
            repo=self.repo,
            payload=attention.attention_payload(
                reason, repo=self.repo, pr=pr, sha=base_sha, message=message
            ),
        )
        notify = self.notify or build_notifier(self.config).notify
        try:
            notify(
                NotificationEvent(
                    event_type=event,
                    severity="error" if event == ATTENTION_EVENT else "info",
                    message=message,
                    pr_number=pr,
                    metadata={"sha": base_sha, "reason": reason},
                )
            )
        except (OSError, RuntimeError) as exc:
            _logger.warning("kew merge queue: attention not delivered: %s", exc)

    def _settle_entry(
        self,
        entry: MergeQueueEntry,
        state: str,
        reason: str,
        done: QueuePass,
        *,
        sha: str | None = None,
        merged_sha: str | None = None,
    ) -> None:
        """Record *entry*'s verdict in the ledger, on the chain, and on the PR.

        *sha* is the head GitHub merged when it is not the queued one, and
        *merged_sha* the commit main landed as.
        """
        if not self.store.settle_merge_entry(
            entry.id, state, reason, report_pending=True
        ):
            return
        done.settled.append((entry, state, reason))
        if state == MERGED:
            done.merged.append(entry)
        emit(
            self.store,
            SETTLED_EVENT,
            actor=Actor(type="policy", id="merge-queue"),
            repo=self.repo,
            payload={
                "pr": entry.pr,
                "sha": sha or entry.head,
                "queued_sha": entry.head,
                "merged_sha": merged_sha or None,
                "state": state,
                "reason": reason,
                "session": entry.session,
            },
        )
        self.say(f"kew merge queue: PR #{entry.pr} {state}: {reason}.")
        pending = next(
            (
                report
                for report in self.store.list_pending_merge_reports(self.repo)
                if report.id == entry.id
            ),
            None,
        )
        if pending is not None:
            self._deliver_verdict(pending)

    @staticmethod
    def _verdict_marker(entry: MergeQueueEntry) -> str:
        if entry.report_token is None:
            raise RuntimeError("pending merge verdict has no delivery token")
        return f"<!-- kew-merge-queue-verdict:{entry.report_token} -->"

    def _deliver_pending_verdicts(self) -> None:
        """Retry every durable terminal verdict before queue processing."""
        for entry in self.store.list_pending_merge_reports(self.repo):
            self._deliver_verdict(entry)

    def _deliver_verdict(self, entry: MergeQueueEntry) -> None:
        """Idempotently post one verdict and confirm its durable delivery."""
        reason = entry.reason or "no reason recorded"
        try:
            self._forge.ensure_comment(
                entry.pr,
                f"kew merge queue: {entry.state} — {reason}.",
                self._verdict_marker(entry),
            )
        except QueueError as exc:
            _logger.warning(
                "kew merge queue: verdict not posted on #%s: %s", entry.pr, exc
            )
            return
        if not self.store.mark_merge_reported(entry.id):
            _logger.warning(
                "kew merge queue: verdict posted on #%s but not marked delivered",
                entry.pr,
            )


# ── the operator's verbs ────────────────────────────────────────────────────


def admission_refusal(
    store: RunStore, pull: PullState, head: str, *, repo: str, base: str
) -> str | None:
    """Why *pull* at *head* may not be queued, or None when it may."""
    if pull.merged or pull.state != "open":
        return f"PR #{pull.number} is {'merged' if pull.merged else pull.state}"
    if pull.draft:
        return f"PR #{pull.number} is a draft"
    if pull.head != head:
        return f"PR #{pull.number}'s head is {pull.head[:12]}, not {head[:12]}"
    bound = binding_refusal(pull, repo, base)
    if bound is not None:
        return bound
    return review_refusal(store, head) or _edited_declaration_refusal(
        store, repo, pull.number, head
    )


def _edited_declaration_refusal(
    store: RunStore, repo: str, pr: int, head: str
) -> str | None:
    """Why *head*, once dropped for an edited declaration, may not return yet.

    The drop asks for a re-review: the edited **Behavior changes** were never
    approved, so the head is queued again only after a review round recorded
    later than the drop found it READY.
    """
    drops = [
        entry.updated_at
        for entry in store.list_merge_queue(repo, include_settled=True)
        if entry.pr == pr and entry.head == head and _edited_drop(entry)
    ]
    if not drops or _reviewed_since(store, head, max(drops)):
        return None
    return (
        f"PR #{pr} at {head[:12]} was dropped because its Behavior changes "
        "were edited after enqueue; queue it again after a new review round "
        "finds it READY"
    )


def enqueue(
    store: RunStore,
    forge: GhForge,
    *,
    pr: int,
    head: str,
    session: str,
    actor: Actor,
    base: str = "main",
) -> tuple[MergeQueueEntry, bool]:
    """Queue *pr* at *head*: refused unless it is open at that head on *base*,
    not a draft, and kew's review found that head READY."""
    sha = checked_head(head)
    pull = forge.pull(pr)
    refusal = admission_refusal(store, pull, sha, repo=forge.repo, base=base)
    if refusal is not None:
        raise QueueError(refusal)
    try:
        entry, created = store.enqueue_merge(
            forge.repo,
            pr,
            sha,
            session=session,
            behavior_changes=tuple(sorted(declared_behavior_changes(pull.body))),
        )
    except ValueError as exc:
        raise QueueError(str(exc)) from exc
    if created:
        emit(
            store,
            ADDED_EVENT,
            actor=actor,
            repo=forge.repo,
            payload={"pr": pr, "sha": sha, "session": session, "entry": entry.id},
        )
    return entry, created


def dequeue(
    store: RunStore, repo: str, *, pr: int, actor: Actor
) -> MergeQueueEntry | None:
    """Take *pr* out of the queue; None when it is not queued.

    Refused while a pass has claimed it: its merge call may already be on its
    way, and the ledger must never say ``removed`` of a PR main holds.
    A terminal landed-tree mismatch remains removable for operator cleanup.
    """
    active = next(
        (entry for entry in store.list_merge_queue(repo) if entry.pr == pr), None
    )
    mismatch = next(
        (
            entry
            for entry in store.list_merge_queue(repo, include_settled=True)
            if entry.pr == pr and entry.state == LANDED_MISMATCH
        ),
        None,
    )
    entry = active or mismatch
    if entry is not None:
        if store.settle_merge_entry(entry.id, REMOVED, f"removed by {actor.id}"):
            emit(
                store,
                REMOVED_EVENT,
                actor=actor,
                repo=repo,
                payload={"pr": pr, "sha": entry.head, "entry": entry.id},
            )
            return entry
        raise QueueError(
            f"PR #{pr} is being merged by {entry.owner or 'a pass'}; "
            "it can no longer be removed"
        )
    return None
