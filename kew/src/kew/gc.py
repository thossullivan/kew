"""`kew gc`: find and remove what closed work left on disk (#2417).

kew keeps checkouts on purpose — a replaced agent checkout is preserved, a
superseded review checkout is moved aside, a sandbox clone outlives its run —
and nothing collected them once their item closed. This module selects those
leftovers and removes exactly what it selected.

Selection is by tracker state, read once per item. An item whose state cannot
be read is kept and named in :attr:`Plan.unreadable`; so is every path of an
open item or of an issue with an open PR, and every path a live reservation, a
holding or deferred run, or a recoverable continuation still holds. A work
root or ledger scope :func:`kew.work_dirs.sweep_work_root` would refuse is not
entered. Removal re-checks the hold once the checkout's claim is held, and
never runs git inside a candidate: a checkout tree goes through
:func:`kew.worktree.remove_checkout_tree` (identity, tree and lock), and a
registered worktree is first unregistered with ``git worktree remove`` run
from the primary checkout.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from kew import subprocess_utils, work_dirs, worktree
from kew.config import DispatchConfig
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.publication_apply import trusted_git_env
from kew.review_checkout import (
    _REVIEW_CHECKOUT_RUN_KINDS,
    CHECKOUT_BUSY,
    ReviewCheckoutError,
    _deferral_with_unknown_checkout,
    _evidenced_deferrals,
    _in_deferred_state,
    review_checkout_path,
)
from kew.review_occupancy import standalone_reservation
from kew.run_store import EpicReservation, RunRecord, RunStore

#: Files kew writes beside a checkout: its claim lock and its generation.
_SIDECARS = (".lock", ".generation")

#: ``(tracker kind, number)`` to ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``;
#: raises :class:`TrackerUnreadable` when the state cannot be read.
TrackerState = Callable[[str, str], str]

#: A head branch to the numbers of the open PRs from it; raises
#: :class:`TrackerUnreadable` when they cannot be read.
OpenPrs = Callable[[str], list[str]]

#: Why the ledger or tracker still holds a checkout now, or ``None``.
Recheck = Callable[["Checkout"], "str | None"]

_PR_URL = re.compile(
    r"https://[^/]+/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>[0-9]+)(?:[/?#]|$)"
)


class TrackerUnreadable(Exception):
    """An item's tracker state could not be read; the message says why."""


@dataclass(frozen=True)
class _Family:
    """One kind of checkout kew leaves under one directory."""

    kind: str
    tracker: str
    pattern: re.Pattern[str]


_AGENT = _Family(
    "agent",
    "issue",
    re.compile(r"issue-(?P<item>[^./]+)(?:\.(?:preserved|replaced)-[^/]+)?"),
)
# ``tempfile.mkdtemp`` appends eight characters from ``[a-z0-9_]``.
_SANDBOX = _Family(
    "sandbox", "issue", re.compile(r"kew-sandbox-(?P<item>[^./]+)-[a-z0-9_]{8}")
)
_REVIEW = _Family("review", "pr", re.compile(r"pr-(?P<item>[0-9]+)(?:\.preserved-.+)?"))


@dataclass(frozen=True)
class Checkout:
    """One checkout location and what exists of it: the directory and the
    lock and generation files beside it."""

    kind: str
    tracker: str
    item: str
    path: Path
    entries: tuple[Path, ...]


@dataclass(frozen=True)
class AgentTag:
    """An agent image tag nothing pins or uses, with the runtime's size."""

    reference: str
    size: str


@dataclass
class Plan:
    """What :func:`select` found: the removable, and what it kept and why."""

    checkouts: list[Checkout] = field(default_factory=list)
    sizes: dict[Path, int] = field(default_factory=dict)
    tags: list[AgentTag] = field(default_factory=list)
    #: Why a checkout was kept, and how many paths that kept.
    kept: dict[str, int] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    def record_unreadable(self, why: str) -> None:
        """Name something that could not be read, once; it is kept."""
        if why not in self.unreadable:
            self.unreadable.append(why)

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())


@dataclass
class Outcome:
    """What :func:`apply` removed, and what it could not."""

    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class _Kept(Exception):
    """A selected checkout that must stay after all; the message says why."""


def github_tracker(repo: str) -> TrackerState:
    """Read an issue's or PR's state with ``gh``, once per item."""
    seen: dict[tuple[str, str], str | TrackerUnreadable] = {}

    def read(kind: str, number: str) -> str:
        key = (kind, number)
        if key not in seen:
            try:
                seen[key] = _read_github_state(repo, kind, number)
            except TrackerUnreadable as exc:
                seen[key] = exc
        state = seen[key]
        if isinstance(state, TrackerUnreadable):
            raise state
        return state

    return read


def _gh_json(repo: str, cmd: list[str]) -> object:
    """``gh``'s JSON answer to *cmd*; raises :class:`TrackerUnreadable`."""
    if repo:
        cmd = [*cmd, "--repo", repo]
    try:
        result = subprocess_utils.run(cmd, check=False, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TrackerUnreadable(f"gh could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:200]
        raise TrackerUnreadable(f"gh exited {result.returncode}: {detail}")
    try:
        return json.loads(result.stdout or "")
    except ValueError as exc:
        raise TrackerUnreadable(f"gh printed no state ({exc})") from exc


def _read_github_state(repo: str, kind: str, number: str) -> str:
    if not number.isdigit():
        raise TrackerUnreadable(f"{number!r} is not an item number")
    answer = _gh_json(repo, ["gh", kind, "view", number, "--json", "state"])
    state = answer.get("state") if isinstance(answer, dict) else None
    if not isinstance(state, str):
        raise TrackerUnreadable("gh printed no state")
    return state.upper()


def github_open_prs(repo: str) -> OpenPrs:
    """Read the open PRs from a head branch with ``gh``, once per branch."""
    seen: dict[str, list[str] | TrackerUnreadable] = {}

    def read(branch: str) -> list[str]:
        if branch not in seen:
            try:
                seen[branch] = _read_open_prs(repo, branch)
            except TrackerUnreadable as exc:
                seen[branch] = exc
        found = seen[branch]
        if isinstance(found, TrackerUnreadable):
            raise found
        return found

    return read


def _read_open_prs(repo: str, branch: str) -> list[str]:
    cmd = ["gh", "pr", "list", "--head", branch, "--state", "open"]
    answer = _gh_json(repo, [*cmd, "--json", "number,state,headRefName"])
    if not isinstance(answer, list):
        raise TrackerUnreadable("gh printed no PR list")
    # Filtered here as well: only an answer naming the branch and OPEN counts.
    return [
        str(pr.get("number"))
        for pr in answer
        if isinstance(pr, dict)
        and pr.get("headRefName") == branch
        and str(pr.get("state", "")).upper() == "OPEN"
    ]


def _ledger_id(store: RunStore, plan: Plan) -> str | None:
    """The ledger's own identity, the scope :func:`work_dirs.sweep_work_root`
    enters; ``None``, with the reason in *plan*, when there is none."""
    try:
        ledger_id = store.read_ledger_id()
    except Exception as exc:  # noqa: BLE001 — unreadable means keep
        plan.record_unreadable(f"run ledger identity could not be read ({exc})")
        return None
    if not ledger_id:
        plan.record_unreadable("the run ledger has no identity: sandbox clones kept")
    return ledger_id or None


def sandbox_scope(store: RunStore, plan: Plan, project: Path) -> Path | None:
    """The ledger's scope of the work root, where its sandbox clones are.

    ``None`` when there is nothing to enter, and also — with the reason in
    *plan*, so every clone is kept and the command exits 2 — when the scope
    is :data:`kew.work_dirs.UNSCOPED`, which every unregistered project
    shares, or when :func:`kew.work_dirs.sweep_work_root` would refuse the
    root (:func:`kew.work_dirs.root_refusal`) or the scope
    (:func:`kew.work_dirs.scope_problem`).
    """
    ledger_id = _ledger_id(store, plan)
    if ledger_id is None:
        return None
    if ledger_id == work_dirs.UNSCOPED:
        plan.record_unreadable(
            f"the run ledger's scope is the shared {work_dirs.UNSCOPED} "
            "directory, which no ledger can vouch for: sandbox clones kept"
        )
        return None
    base = work_dirs.configured_root()
    if not os.path.lexists(base):
        return None
    refusal = work_dirs.root_refusal(base, project)
    if refusal is not None:
        plan.record_unreadable(f"work root refused ({refusal}): sandbox clones kept")
        return None
    scoped = base / ledger_id
    if not os.path.lexists(scoped):
        return None
    problem = work_dirs.scope_problem(base, scoped)
    if problem is not None:
        plan.record_unreadable(f"ledger scope refused ({problem}): sandbox clones kept")
        return None
    return scoped


def checkout_roots(
    config: DispatchConfig, scope: Path | None
) -> list[tuple[Path, _Family]]:
    """Where each kind of checkout lives: the agent and review directories
    under ``worktree_base``, and the ledger's *scope* of the work root."""
    base = Path(config.branches.worktree_base).expanduser()
    roots = [(base / "agent", _AGENT)]
    if scope is not None:
        roots.append((scope, _SANDBOX))
    roots.append((review_checkout_path(config, 0).parent, _REVIEW))
    return roots


def _checkout_name(entry: os.DirEntry[str]) -> str:
    for suffix in _SIDECARS:
        if entry.name.endswith(suffix):
            return entry.name[: -len(suffix)]
    return entry.name


def _belongs(entry: os.DirEntry[str], name: str) -> bool:
    """A real directory for the checkout itself, a regular file for a
    sidecar; a symlink is never followed or removed."""
    if entry.name == name:
        return entry.is_dir(follow_symlinks=False)
    return entry.is_file(follow_symlinks=False)


def scan(parent: Path, family: _Family) -> list[Checkout]:
    """Every checkout of *family* directly under *parent*. A checkout whose
    own entry is not a real directory, or one of whose sidecars is not a
    regular file, is no checkout: all its entries stay, since claiming or
    removing it would resolve the odd one."""
    if parent.is_symlink() or not parent.is_dir():
        return []
    groups: dict[str, list[Path]] = {}
    foreign: set[str] = set()
    with os.scandir(parent) as listing:
        for entry in sorted(listing, key=lambda e: e.name):
            name = _checkout_name(entry)
            if family.pattern.fullmatch(name) is None:
                continue
            if _belongs(entry, name):
                groups.setdefault(name, []).append(Path(entry.path))
            else:
                foreign.add(name)
    found: list[Checkout] = []
    for name, entries in groups.items():
        if name in foreign:
            continue
        match = family.pattern.fullmatch(name)
        assert match is not None
        found.append(
            Checkout(
                family.kind,
                family.tracker,
                match.group("item"),
                parent / name,
                tuple(entries),
            )
        )
    return found


def _entry_size(path: Path) -> int:
    """Apparent bytes under *path*, never following a symlink; raises
    :class:`OSError` when *path* itself cannot be read."""
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        return info.st_size
    total = 0
    for root, dirs, files in os.walk(path):
        for name in [*files, *(d for d in dirs if os.path.islink(Path(root, d)))]:
            with contextlib.suppress(OSError):
                total += os.lstat(Path(root, name)).st_size
    return total


def _run_state(run: RunRecord) -> str:
    category = run.error_category or ""
    return f"{run.status}, {category}" if category else str(run.status)


def _holding_run(runs: list[RunRecord]) -> str | None:
    """Why one of *runs* holds its tree (:func:`work_dirs.run_holds_tree`, or
    a deferred publication), or ``None``."""
    for run in runs:
        if work_dirs.run_holds_tree(run) or _in_deferred_state(run):
            return f"run {run.run_id} ({_run_state(run)}) holds it"
    return None


def _item_hold(store: RunStore, item: str) -> str | None:
    """Why the ledger still holds *item*'s checkouts, or ``None``."""
    if store.item_reservation(item) is not None:
        return "a live reservation holds it"
    held = _holding_run(store.list_by_item(item))
    if held is not None:
        return held
    for record in store.list_all_continuations():
        if (
            record.item_id == item
            and not store.continuation_was_abandoned(record)
            and not store.continuation_is_complete(record)
        ):
            return "a continuation can still recover its work"
    return None


def _review_hold(store: RunStore, pr: str) -> str | None:
    """Why the ledger holds every review checkout of PR *pr*: a review
    reservation of it, or a review run holding its tree that is not a
    deferral (a deferral holds only the copy it names)."""
    runs = [
        run
        for run in store.list_by_issue(int(pr))
        if run.run_kind in _REVIEW_CHECKOUT_RUN_KINDS
    ]
    reserved = _review_reservation_hold(store, pr, runs)
    if reserved is not None:
        return reserved
    return _holding_run([run for run in runs if not _in_deferred_state(run)])


def _review_reservation_hold(
    store: RunStore, pr: str, runs: list[RunRecord]
) -> str | None:
    """Which live reservation holds PR *pr*'s review checkouts, or ``None``.

    kew review reserves the node or the PR's owner issue, never the PR: the
    items its *runs* recorded, and the reservations that name the PR."""
    items = {pr, *(run.item_id for run in runs if run.item_id)}
    if any(store.item_reservation(item) is not None for item in sorted(items)):
        return "a live reservation holds it"
    if any(
        _reserves_pr(store, reservation, int(pr))
        for reservation in store.list_epic_reservations(REVIEW_EPIC_REF)
    ):
        return "a live review reservation holds it"
    return None


def _reserves_pr(store: RunStore, reservation: EpicReservation, pr: int) -> bool:
    """Whether *reservation* is a review of PR *pr*: a hand-run review's
    record names it, or the reserved run is a review of it."""
    event = standalone_reservation(store, reservation.run_id)
    if event is not None and (event.payload or {}).get("pr_number") == pr:
        return True
    run = store.get(reservation.run_id)
    return (
        run is not None
        and run.issue_num == pr
        and run.run_kind in _REVIEW_CHECKOUT_RUN_KINDS
    )


def _deferred_copies(store: RunStore, pr: int) -> set[Path] | None:
    """The copies PR *pr*'s deferred publications name, or ``None`` when a
    deferral's copy is unknown; raises when the ledger cannot be read."""
    if _deferral_with_unknown_checkout(store, pr) is not None:
        return None
    return {
        deferral.path.resolve(strict=False)
        for deferral in _evidenced_deferrals(store, pr)
    }


def _label(checkout: Checkout) -> str:
    noun = "issue" if checkout.tracker == "issue" else "PR"
    return f"{noun} #{checkout.item}"


class _Selector:
    """Decide each checkout, reading each item's state once."""

    def __init__(
        self,
        store: RunStore,
        tracker: TrackerState,
        open_prs: OpenPrs,
        config: DispatchConfig,
    ) -> None:
        self.store = store
        self.tracker = tracker
        self.open_prs = open_prs
        self.branch_prefix = config.branches.prefix
        self.repo = config.project.repo
        self.review_protected: dict[str, set[Path] | None] = {}
        #: What the last :meth:`hold` could not read.
        self.unreadable: list[str] = []

    def consider(self, checkout: Checkout, plan: Plan) -> None:
        reason = self.hold(checkout)
        for problem in self.unreadable:
            plan.record_unreadable(problem)
        if reason is not None:
            plan.kept[reason] = plan.kept.get(reason, 0) + len(checkout.entries)
            return
        try:
            sizes = {entry: _entry_size(entry) for entry in checkout.entries}
        except OSError as exc:
            plan.record_unreadable(f"{checkout.path}: could not be read ({exc})")
            plan.kept["unreadable on disk"] = plan.kept.get(
                "unreadable on disk", 0
            ) + len(checkout.entries)
        else:
            plan.checkouts.append(checkout)
            plan.sizes.update(sizes)

    def hold(self, checkout: Checkout) -> str | None:
        """Why *checkout* must be kept, or ``None`` when nothing holds it."""
        self.unreadable = []
        try:
            state = self.tracker(checkout.tracker, checkout.item)
        except TrackerUnreadable as exc:
            message = f"{_label(checkout)}: tracker state could not be read ({exc})"
            self.unreadable.append(message)
            return f"{_label(checkout)} state unreadable"
        if state == "OPEN":
            return f"{_label(checkout)} is open"
        if state not in {"CLOSED", "MERGED"}:
            return f"{_label(checkout)} is {state.lower()}"
        reason = self._read_hold(checkout)
        return None if reason is None else f"{_label(checkout)}: {reason}"

    def _read_hold(self, checkout: Checkout) -> str | None:
        try:
            return self._ledger_hold(checkout)
        except TrackerUnreadable as exc:
            label = _label(checkout)
            self.unreadable.append(f"{label}: open PRs could not be read ({exc})")
            return "its open PRs could not be read"
        except Exception as exc:  # noqa: BLE001 — unreadable means keep
            self.unreadable.append(f"run ledger could not be read ({exc})")
            return "the run ledger could not be read"

    def _ledger_hold(self, checkout: Checkout) -> str | None:
        if checkout.kind == "review":
            held = _review_hold(self.store, checkout.item)
            return held or self._deferral_hold(checkout)
        return _item_hold(self.store, checkout.item) or self._open_pr_hold(
            checkout.item
        )

    def _open_pr_hold(self, item: str) -> str | None:
        """An open PR keeps its issue's checkouts: one from the issue's
        branch, or one a run of the item recorded. A recorded PR of another
        repository cannot be read here, so it keeps them too."""
        found = self.open_prs(f"{self.branch_prefix}{item}")
        if found:
            return f"PR #{found[0]} is open"
        urls = {run.pr_url for run in self.store.list_by_item(item) if run.pr_url}
        for url in sorted(urls):
            number = self._own_pr_number(url)
            if number is None:
                repo = self.repo or "this repository"
                return f"a run recorded {url}, not a PR of {repo}"
            if self.tracker("pr", number) == "OPEN":
                return f"PR #{number} is open"
        return None

    def _own_pr_number(self, url: str) -> str | None:
        """The number of the PR *url* names in this repository, or ``None``."""
        match = _PR_URL.match(url)
        if match is None or not self.repo:
            return None
        if match.group("repo").lower() != self.repo.lower():
            return None
        return match.group("number")

    def _deferral_hold(self, checkout: Checkout) -> str | None:
        if checkout.item not in self.review_protected:
            # A deferral's copy is kept whatever became of the PR.
            self.review_protected[checkout.item] = _deferred_copies(
                self.store, int(checkout.item)
            )
        protected = self.review_protected[checkout.item]
        if protected is None:
            return "a deferred publication's copy cannot be told apart"
        if checkout.path.resolve(strict=False) in protected:
            return "a deferred publication names it"
        return None


def select(
    config: DispatchConfig,
    store: RunStore,
    tracker: TrackerState,
    tags: list[AgentTag],
    *,
    open_prs: OpenPrs,
    project: Path,
) -> Plan:
    """Every checkout of a closed item nothing in the ledger holds, and *tags*.

    *project* is the checkout kew runs for, which the work root must not
    contain (:func:`kew.work_dirs.root_refusal`).
    """
    plan = Plan(tags=list(tags))
    selector = _Selector(store, tracker, open_prs, config)
    scope = sandbox_scope(store, plan, project)
    for parent, family in checkout_roots(config, scope):
        for checkout in scan(parent, family):
            selector.consider(checkout, plan)
    return plan


def recheck(
    config: DispatchConfig,
    store: RunStore,
    tracker: TrackerState,
    open_prs: OpenPrs,
) -> Recheck:
    """The hold :func:`select` applied, with the ledger read again, for
    :func:`apply` to ask once it holds a checkout's claim."""

    def hold(checkout: Checkout) -> str | None:
        selector = _Selector(store, tracker, open_prs, config)
        reason = selector.hold(checkout)
        if reason is not None and selector.unreadable:
            return f"{reason} ({'; '.join(selector.unreadable)})"
        return reason

    return hold


def _registered_worktrees(repo_root: Path) -> tuple[set[Path], str | None]:
    """The worktrees the primary checkout has registered, read from there,
    or why they could not be read."""
    try:
        result = subprocess_utils.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain", "-z"],
            check=False,
            env=trusted_git_env(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return set(), f"git worktree list could not run ({exc})"
    if result.returncode != 0:
        return set(), f"git worktree list failed ({_git_detail(result)})"
    return {
        Path(field[len("worktree ") :]).resolve(strict=False)
        for field in (result.stdout or "").split("\0")
        if field.startswith("worktree ")
    }, None


def _git_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()[:200]
    return f"exit {result.returncode}" + (f": {detail}" if detail else "")


def _claim_review_lock(review_dir: Path, lock: Path) -> IO[str]:
    """Take the review claim :func:`kew.review_checkout._claim` takes, on a
    regular file only: a lock that is a symlink, or anything but a regular
    file, is refused before it is opened, and never opened through."""
    if os.path.islink(lock):
        raise _Kept(f"{lock} is a symlink")
    if os.path.lexists(lock) and not stat.S_ISREG(os.lstat(lock).st_mode):
        raise _Kept(f"{lock} is not a regular file")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(lock, flags, 0o644)
    except OSError as exc:
        raise _Kept(f"{lock} could not be opened ({exc})") from exc
    handle = os.fdopen(fd, "a+", encoding="utf-8")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Kept(f"{lock} is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ReviewCheckoutError(
            CHECKOUT_BUSY,
            f"review checkout {review_dir} is claimed by another reviewer",
        ) from exc
    except BaseException:
        handle.close()
        raise
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def _still_holds(handle: IO[str], lock: Path) -> bool:
    """Whether *handle* is the regular file at *lock* now, not a link to it."""
    return not os.path.islink(lock) and worktree._holds_the_file_at(handle, lock)


@contextlib.contextmanager
def _review_claim(checkout: Checkout) -> Iterator[Path | None]:
    """Hold the PR's review claim around a review checkout's removal, and
    yield the PR's review directory.

    The claim must be on the file at the lock's path: a lock that is not a
    regular file is never claimed, and one replaced between the open and the
    ``flock`` is refused. The lock is unlinked only while the claim still
    holds the file at that path, and only when the claim created it or the
    review directory is gone, so no other reviewer's lock is ever unlinked.
    """
    if checkout.kind != "review":
        yield None
        return
    review_dir = checkout.path.parent / f"pr-{checkout.item}"
    lock = Path(f"{review_dir}.lock")
    existed = os.path.lexists(lock)
    handle = _claim_review_lock(review_dir, lock)
    try:
        if not _still_holds(handle, lock):
            raise ReviewCheckoutError(
                CHECKOUT_BUSY, f"{lock} was replaced while it was being claimed"
            )
        yield review_dir
    finally:
        gone = not os.path.lexists(review_dir)
        if (not existed or gone) and _still_holds(handle, lock):
            lock.unlink(missing_ok=True)
        handle.close()


class _Remover:
    """Remove a plan's checkouts and tags, recording each result."""

    def __init__(self, repo_root: Path, outcome: Outcome, hold: Recheck) -> None:
        self.repo_root = repo_root
        self.registered, self.unlisted = _registered_worktrees(repo_root)
        self.outcome = outcome
        self.hold = hold
        self.unregistered = False

    def checkout(self, checkout: Checkout) -> None:
        if self.unlisted is not None:
            # Removing a tree git still lists would orphan its registration.
            self.outcome.failed.append(f"{checkout.path}: kept, {self.unlisted}")
            return
        try:
            with _review_claim(checkout) as review_dir:
                self._remove(checkout, review_dir)
        except (_Kept, ReviewCheckoutError, worktree.CheckoutBusy) as exc:
            self.outcome.failed.append(f"{checkout.path}: kept, {exc}")
        except (OSError, RuntimeError) as exc:
            self.outcome.failed.append(f"{checkout.path}: {exc}")
        else:
            self._account(checkout)

    def _account(self, checkout: Checkout) -> None:
        """Record each entry as removed, or as still present."""
        left = [entry for entry in checkout.entries if os.path.lexists(entry)]
        for entry in checkout.entries:
            if entry in left:
                self.outcome.failed.append(f"{entry}: still present after removal")
            else:
                self.outcome.removed.append(str(entry))

    def _remove(self, checkout: Checkout, review_dir: Path | None) -> None:
        path = checkout.path
        # Unregistering or revoking would act on the link's target, and a
        # claim writes through a linked lock.
        _refuse_links(path)
        if path != review_dir:
            # The review directory's lock is the claim held above.
            worktree.claim_checkout(path)
        try:
            reason = self.hold(checkout)
            if reason is not None:
                raise _Kept(f"held once claimed: {reason}")
            # Replaced by a link since the check above, before the claim.
            _refuse_links(path)
            self._unregister(path)
        except BaseException:
            worktree.release_checkout_claim(path)
            raise
        worktree.remove_checkout_tree(path)

    def _unregister(self, path: Path) -> None:
        """Unregister *path* from the primary checkout when it is listed
        there; a failure keeps the tree, so no registration is orphaned."""
        if path.resolve(strict=False) not in self.registered:
            return
        try:
            result = subprocess_utils.run(
                ["git", "-C", str(self.repo_root), "worktree", "remove", "--force"]
                + [str(path)],
                check=False,
                env=trusted_git_env(),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise _Kept(f"git worktree remove could not run ({exc})") from exc
        self.unregistered = True
        if result.returncode != 0:
            raise _Kept(f"git worktree remove failed ({_git_detail(result)})")

    def finish(self) -> None:
        if not self.unregistered:
            return
        try:
            result = subprocess_utils.run(
                ["git", "-C", str(self.repo_root), "worktree", "prune"],
                check=False,
                env=trusted_git_env(),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.outcome.failed.append(f"git worktree prune could not run ({exc})")
        else:
            if result.returncode != 0:
                self.outcome.failed.append(
                    f"git worktree prune failed ({_git_detail(result)})"
                )

    def tag(self, runtime: str, tag: AgentTag) -> None:
        try:
            result = subprocess_utils.run(
                [runtime, "image", "rm", "--", tag.reference], check=False, timeout=60
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.outcome.failed.append(f"image {tag.reference}: {exc}")
        else:
            self._account_tag(tag, result)

    def _account_tag(
        self, tag: AgentTag, result: subprocess.CompletedProcess[str]
    ) -> None:
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[:200]
            self.outcome.failed.append(f"image {tag.reference}: {detail}")
            return
        self.outcome.removed.append(f"image {tag.reference}")


def _refuse_links(path: Path) -> None:
    """Keep *path* when it, or a sidecar beside it, is a symlink."""
    for entry in (path, *(Path(f"{path}{suffix}") for suffix in _SIDECARS)):
        if os.path.islink(entry):
            raise _Kept(f"{entry} is a symlink")


def apply(plan: Plan, *, runtime: str, repo_root: Path, hold: Recheck) -> Outcome:
    """Remove exactly what *plan* lists. *repo_root* is the project's git
    checkout, which the worktrees are registered in. A checkout is kept and
    reported when a live run claims it, when *hold* names a reason once its
    claim is held, or when git cannot read or remove its registration."""
    outcome = Outcome()
    remover = _Remover(repo_root, outcome, hold)
    for checkout in plan.checkouts:
        remover.checkout(checkout)
    remover.finish()
    for tag in plan.tags:
        remover.tag(runtime, tag)
    return outcome
