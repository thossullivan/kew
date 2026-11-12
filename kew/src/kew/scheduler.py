"""Parallel dispatch scheduler with ProcessPoolExecutor.

Manages a pool of workers, tracks in-flight issues, and orchestrates
the main dispatch loop for parallel execution.
"""

import json
import signal
import subprocess
import time
import types
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import audit
from kew.audit import Actor
from kew.command_outcome import CommandOutcome
from kew.config import DispatchConfig, project_root
from kew.ids import branch_slug
from kew.labels import (
    ClaimResult,
    _update_labels,
    mark_failed,
    rollback_claim,
    try_claim_issue,
)
from kew.ledger_identity import canonical_project_root, open_managed_store
from kew.models import GhIssue
from kew.overlap import (
    CLAIM_ROLLBACK_FAILED,
    DispatchOutcome,
    OverlapCoordinator,
    OverlapDecision,
    build_identifier_index,
    build_module_index,
)
from kew.progress import _error, _log, _success, _warn
from kew.prompt import build_prompt
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL
from kew.runners.registry import resolve_runner
from kew.selector import (
    GhQueryError,
    _has_epic_label,
    apply_cache_reorder,
    empty_queue_message,
    get_complexity,
    list_ready_issues,
    select_next_issue,
)
from kew.service.prompt_profiles import launch_addendum
from kew.subprocess_utils import TOOL_PATHS
from kew.subprocess_utils import run as subprocess_run
from kew.worktree import remote_branch_exists

if TYPE_CHECKING:
    from kew.run_store import RunRecord, SqliteRunStore


def _open_scheduler_store(config: DispatchConfig) -> "SqliteRunStore":
    """Open the scheduler's writable store through the ledger registry."""
    root = canonical_project_root(project_root())
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    return open_managed_store(
        project_root=root,
        db_path=db_path,
        anchors_path=root / audit.DEFAULT_ANCHORS_PATH,
        read_only=False,
        check_same_thread=True,
    )


@dataclass(frozen=True)
class DrainResult:
    """Retained fleet results; only the command boundary assigns public codes."""

    claim_failures: tuple[tuple[int, ClaimResult], ...] = ()
    dispatch_outcomes: tuple[tuple[int, DispatchOutcome], ...] = ()
    resolve_outcomes: tuple[tuple[int, CommandOutcome], ...] = ()
    required_failures: tuple[tuple[str, str], ...] = ()


class DispatchScheduler:
    """Orchestrates parallel dispatch of GitHub issues to Claude Code workers."""

    # How long (seconds) a permanently-skippable issue — e.g. one whose remote
    # branch already exists — is held out of selection before being retried.
    # Without this, the loop re-selects the same undispatchable issue every
    # iteration with no sleep, hammering the gh API (#SFD-151, I9).
    _SKIP_BACKOFF_SECONDS: float = 300.0
    _STALE_PENDING_SWEEP_SECONDS: float = 60.0
    # Consecutive 60s durable-capacity waits before the loop gives up its
    # turn: other actors' runs hold every slot and this process cannot
    # select, so it exits naming the holders instead of idling until the
    # 24-hour stale-pending backstop.
    _CAPACITY_WAIT_CHECKS: int = 10

    def __init__(
        self,
        config: DispatchConfig,
        *,
        store: "SqliteRunStore | None" = None,
    ) -> None:
        self.config = config
        self.store = store
        self.max_workers: int = config.fleet_concurrency()
        self.in_flight: set[int] = set()
        self.resolve_in_flight: set[int] = set()
        self._shutdown: bool = False
        self.coordinator = OverlapCoordinator()
        # issue_num -> monotonic deadline until which the issue stays skipped
        self._skip_backoff: dict[int, float] = {}
        self._pending_liveness: Callable[[RunRecord], bool | None] | None = None
        self._last_capacity_holders: tuple[tuple[str, str], ...] | None = None
        # reason -> issue numbers hidden from the last selection pass
        self._last_exclusions: dict[str, set[int]] = {}
        # monotonic deadline of the next age-gated stale-pending sweep
        self._next_stale_pending_sweep = 0.0
        # whether the current empty-queue streak already printed the
        # 'label an issue ready' teaching message
        self._empty_teaching_shown = False
        # PR number -> the item key its resolve worker's pending row carries,
        # so a live resolve child is counted once (row or starting child).
        self._resolve_items: dict[int, str] = {}
        # consecutive passes that waited on a full durable fleet
        self._durable_full_checks = 0
        self._claim_failures: list[tuple[int, ClaimResult]] = []
        self._dispatch_outcomes: list[tuple[int, DispatchOutcome]] = []
        self._resolve_outcomes: list[tuple[int, CommandOutcome]] = []
        self._required_failures: list[tuple[str, str]] = []
        self._capacity_read_failed = False

    def has_capacity(self) -> bool:
        """Return True if there are available worker slots."""
        return (len(self.in_flight) + len(self.resolve_in_flight)) < self.max_workers

    @property
    def available_slots(self) -> int:
        """Number of free worker slots."""
        return self.max_workers - len(self.in_flight) - len(self.resolve_in_flight)

    def is_in_flight(self, issue_num: int) -> bool:
        """Check whether an issue is currently being worked on."""
        return issue_num in self.in_flight

    def is_resolve_in_flight(self, pr_number: int) -> bool:
        """Check whether a PR is currently being resolved."""
        return pr_number in self.resolve_in_flight

    def _poll_conflicting_prs(self) -> list[dict[str, Any]]:
        """Query GitHub for open agent PRs with merge conflicts.

        Returns only PRs that are CONFLICTING and not already being resolved.
        Returns an empty list when resolve.auto is disabled or on error.
        """
        if not self.config.resolve.auto:
            return []

        try:
            result = subprocess_run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--limit",
                    "1000",
                    "--repo",
                    self.config.project.repo,
                    "--label",
                    self.config.labels.agent_pr,
                    "--state",
                    "open",
                    "--json",
                    "number,headRefName,baseRefName,title,body,state,mergeable,labels",
                ],
                check=True,
            )
            prs: list[dict[str, Any]] = json.loads(result.stdout)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            self._required_failures.append(("Conflict PR query", str(exc)))
            _warn(f"Failed to poll PRs for conflicts: {exc}")
            return []

        return [
            pr
            for pr in prs
            if pr.get("mergeable") == "CONFLICTING"
            and pr.get("number") not in self.resolve_in_flight
            and not _has_epic_label(pr.get("labels", []))
        ]

    def _reap_resolve_completed(
        self, futures: dict[Future[CommandOutcome], int]
    ) -> None:
        """Check done resolve futures, remove from resolve_in_flight."""
        done = [f for f in futures if f.done()]
        for fut in done:
            pr_number = futures.pop(fut)
            self.resolve_in_flight.discard(pr_number)
            self._resolve_items.pop(pr_number, None)
            try:
                outcome = fut.result()
                self._resolve_outcomes.append((pr_number, outcome))
                if outcome.kind == "not_run":
                    _warn(f"Resolve worker for PR #{pr_number} could not run.")
                elif outcome.exit_code == 0:
                    _success(f"Resolve worker for PR #{pr_number} completed.")
                else:
                    _warn(
                        f"Resolve worker for PR #{pr_number} "
                        f"exited with code {outcome.exit_code}."
                    )
            except Exception as exc:
                self._required_failures.append((f"Resolve PR #{pr_number}", str(exc)))
                _error(f"Resolve worker for PR #{pr_number} raised: {exc}")

    def _cleanup_stale_pending(self) -> None:
        """Reap PENDING rows left behind by a crashed dispatch (#SFD-152).

        Only the store's 24-hour age backstop changes pending rows.
        Store errors never abort the loop.
        """
        try:
            if self.store is not None:
                cleaned = self.store.cleanup_stale_pending()
            else:
                db_path = Path(self.config.logging.dir).resolve() / "dispatch.db"
                if not db_path.exists():
                    return
                with _open_scheduler_store(self.config) as store:
                    cleaned = store.cleanup_stale_pending()
        except Exception as exc:
            _warn(f"Failed to clean up stale pending records: {exc}")
            return
        if cleaned:
            _warn(
                f"Cleaned up {cleaned} stale pending record(s) from a previous crash."
            )

    def _cleanup_stale_pending_if_due(self, next_sweep: float) -> float:
        """Run periodic stale-row cleanup and return the next deadline."""
        now = time.monotonic()
        if now < next_sweep:
            return next_sweep
        self._cleanup_stale_pending()
        return now + self._STALE_PENDING_SWEEP_SECONDS

    def select_available_issues(
        self,
        max_count: int,
        force_issue: int | None = None,
    ) -> list[GhIssue]:
        """Select issues while excluding local children and pending run rows.

        Each selected issue is added to the exclude set so subsequent
        calls within the same batch don't re-select it.
        """
        selected: list[GhIssue] = []
        # Remember why each number is hidden so an empty selection can say
        # "held" instead of "empty" (one selector query per pass, no re-poll).
        # Insertion order is the precedence _excluded_reasons documents.
        self._last_exclusions = {
            "live child": set(self.in_flight),
            "pending run": self._pending_issue_numbers(),
            "skip backoff": self._active_skips(),
        }
        exclude: set[int] = set().union(*self._last_exclusions.values())

        for _ in range(max_count):
            issue = select_next_issue(
                config=self.config,
                force_issue=force_issue,
                exclude=exclude if exclude else None,
            )
            if issue is None:
                break
            num = issue.get("number")
            if not isinstance(num, int):
                break
            exclude.add(num)
            selected.append(issue)
            if force_issue is not None:
                break

        return self._cache_reorder_batch(selected)

    def _pending_issue_numbers(self) -> set[int]:
        """Return GitHub issues whose durable pending rows may still be live."""
        if self.store is None:
            return set()
        try:
            from kew.graph.driver.tick import default_run_liveness

            liveness = self._pending_liveness or default_run_liveness(self.config)
            return {
                run.issue_num
                for run in self.store.list_pending()
                if run.issue_num > 0 and liveness(run) is not False
            }
        except Exception as exc:
            _warn(f"Failed to read pending worker issues: {exc}")
            return set()

    def _fleet_available_slots(self, *, log_holders: bool = True) -> int:
        """Return slots after durable runs and starting children consume capacity.

        ``log_holders=False`` skips the capacity-full log (a preview exits
        instead of retrying, so 'Retrying in 60s' would be false there).
        """
        local_only = max(0, self.available_slots)
        self._capacity_read_failed = False
        if self.store is None:
            return local_only
        try:
            from kew.graph.driver.tick import default_run_liveness

            probe = default_run_liveness(self.config)
            verdicts: dict[str, bool | None] = {}

            def liveness(run: "RunRecord") -> bool | None:
                if run.run_id not in verdicts:
                    verdicts[run.run_id] = probe(run)
                return verdicts[run.run_id]

            self._pending_liveness = liveness
            pending = self.store.list_pending()
            live = self.store.live_run_count(liveness=liveness)
            live_pending_items = {
                run.item_id or str(run.issue_num)
                for run in pending
                if liveness(run) is not False
            }
            starting_children = sum(
                str(issue_num) not in live_pending_items for issue_num in self.in_flight
            )
            # A resolve child whose run row is already pending is inside
            # ``live``; only a child still starting (no row yet) is added.
            starting_resolves = sum(
                self._resolve_items.get(pr, f"PR #{pr}") not in live_pending_items
                for pr in self.resolve_in_flight
            )
            occupied = live + starting_children + starting_resolves
            slots = max(0, self.max_workers - occupied)
        except Exception as exc:
            self._capacity_read_failed = True
            self._required_failures.append(("Fleet worker capacity", str(exc)))
            _warn(f"Failed to read fleet worker capacity: {exc}")
            return 0
        # Log-only: a failure describing the holders must never discard the
        # computed verdict (a 'fleet full' result falling back to local
        # capacity would re-open the over-admission this PR closes).
        if not log_holders:
            return slots
        try:
            self._log_fleet_capacity(slots, pending, liveness)
        except Exception as exc:
            _warn(f"Failed to describe fleet capacity holders: {exc}")
        return slots

    def _additional_capacity_holders(
        self, live_items: set[str]
    ) -> set[tuple[str, str]]:
        """Describe reservations without run rows and local child capacity.

        ``live_items`` are the pending rows whose probe is not ``False``, the
        same set the slot count uses: a child whose own row already probes
        dead still occupies a slot as a starting child, so it must be named
        here rather than vanish behind its dead row.
        """
        holders = self._reservation_holders(live_items)
        holders.update(self._starting_child_holders(live_items))
        return holders

    def _reservation_holders(self, pending_items: set[str]) -> set[tuple[str, str]]:
        """Epic reservations that hold a slot without a pending run row."""
        assert self.store is not None
        return {
            (reservation.item_id, "unprobeable")
            for epic in self.store.live_reservation_counts_by_epic()
            for reservation in self.store.list_epic_reservations(epic)
            if reservation.item_id not in pending_items
        }

    def _starting_child_holders(self, pending_items: set[str]) -> set[tuple[str, str]]:
        """Local children that have not yet written their pending row.

        A child that already owns a pending row is described by that row's
        verdict, so only a child still starting is named here.
        """
        holders = {
            (str(number), "live")
            for number in self.in_flight
            if str(number) not in pending_items
        }
        holders.update(
            (f"PR #{number}", "live")
            for number in self.resolve_in_flight
            if self._resolve_items.get(number, f"PR #{number}") not in pending_items
        )
        return holders

    def _log_fleet_capacity(
        self,
        slots: int,
        pending: list["RunRecord"],
        liveness: Callable[["RunRecord"], bool | None],
    ) -> None:
        """Log capacity holders only when the full state changes."""
        if slots > 0:
            self._last_capacity_holders = None
            return
        live_rows = {
            (run.item_id or str(run.issue_num)): liveness(run) is True
            for run in pending
            if liveness(run) is not False
        }
        holders = tuple(
            sorted(
                {
                    (item, "live" if alive else "unprobeable")
                    for item, alive in live_rows.items()
                }
                | self._additional_capacity_holders(set(live_rows))
            )
        )
        if holders == self._last_capacity_holders:
            return
        self._last_capacity_holders = holders
        detail = ", ".join(f"{item} ({verdict})" for item, verdict in holders)
        _log(
            f"Fleet capacity full. Holders: {detail or 'none'}. Retrying in 60s. "
            "A holder keeps its slot until its run finishes or the 24-hour "
            "stale-pending backstop reclaims it (`kew cleanup` runs that same "
            "sweep; it frees nothing younger than 24 hours)."
        )

    def _active_skips(self) -> set[int]:
        """Issue numbers still inside their skip-backoff window.

        Expired entries are pruned so a skip only suppresses re-selection for
        ``_SKIP_BACKOFF_SECONDS``, after which the issue is retried once (the
        remote branch may have since been cleaned up).
        """
        now = time.monotonic()
        expired = [num for num, until in self._skip_backoff.items() if until <= now]
        for num in expired:
            del self._skip_backoff[num]
        return set(self._skip_backoff)

    def _record_skip(self, num: int) -> None:
        """Hold *num* out of selection for the backoff window."""
        self._skip_backoff[num] = time.monotonic() + self._SKIP_BACKOFF_SECONDS

    def _excluded_reasons(self) -> dict[int, str]:
        """Issue number -> the first reason it was hidden from selection.

        A parent's own running child is both in ``in_flight`` and the owner
        of a live pending row, so ``_last_exclusions`` insertion order (live
        child, pending run, skip backoff) decides which reason is named.
        """
        reasons: dict[int, str] = {}
        for reason, numbers in self._last_exclusions.items():
            for number in numbers:
                reasons.setdefault(number, reason)
        return reasons

    def _held_issue_detail(self, force_issue: int | None = None) -> str:
        """Describe the ready candidates the last selection pass hid, or ''.

        An exclusion only counts as a hold when the hidden number is a real
        selection candidate: a pending row for an issue that is not labeled
        ready hides nothing, and naming it would relabel an empty queue as
        held work. The candidate list is read once per no-selection pass
        (never on a pass that selected work), so a streak costs at most
        three extra tracker reads before the loop exits.
        """
        reasons = self._excluded_reasons()
        if not reasons:
            return ""
        if force_issue is not None:
            candidates = {force_issue} if force_issue in reasons else set()
        else:
            try:
                candidates = {
                    number
                    for issue in list_ready_issues(config=self.config)
                    if isinstance(number := issue.get("number"), int)
                }
            except Exception as exc:
                _warn(f"Could not read ready candidates to explain the hold: {exc}")
                return ""
        return ", ".join(
            f"#{number} ({reason})"
            for number, reason in sorted(reasons.items())
            if number in candidates
        )

    def _log_empty_queue(self, consecutive_empty: int, held: str) -> None:
        """Log the no-selection state during the loop.

        A truly empty queue prints the teaching message showing how to label
        an issue ready (CS-2, CS-6) once per streak, on the first check whose
        candidates were not held. A pass whose ready candidates were hidden
        by a live child, a pending run row, or a skip backoff names those
        holds instead: the issues are already ready, so telling the operator
        to label them would be false.
        """
        if held:
            _log(
                f"No selectable issue (held: {held}). "
                f"Empty {consecutive_empty}/3. Retrying in 60s..."
            )
            return
        if not self._empty_teaching_shown:
            self._empty_teaching_shown = True
            _log(empty_queue_message(self.config))
        _log(f"Empty {consecutive_empty}/3. Retrying in 60s...")

    def _handle_no_selection(
        self, consecutive_empty: int, force_issue: int | None = None
    ) -> int:
        """Advance the empty streak after every selection without a candidate."""
        consecutive_empty += 1
        held = self._held_issue_detail(force_issue)
        if consecutive_empty >= 3:
            if held:
                _log(f"No selectable issue after 3 checks (held: {held}). Exiting.")
            else:
                _log("Queue empty after 3 checks. Exiting.")
            return consecutive_empty
        self._log_empty_queue(consecutive_empty, held)
        time.sleep(60)
        return consecutive_empty

    def _cache_reorder_batch(self, issues: list[GhIssue]) -> list[GhIssue]:
        """Apply cache-aware reorder to the selected batch before dispatch."""
        cache_cfg = self.config.routing.cache if self.config.routing else None
        if cache_cfg is None or len(issues) <= 1:
            return issues
        try:
            with (
                nullcontext(self.store)
                if self.store is not None
                else _open_scheduler_store(self.config)
            ) as store:
                return apply_cache_reorder(issues, self.config, store=store)
        except Exception:
            _warn("Cache reorder failed, using original order")
            return issues

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _strip_stale_overlap_holds(self) -> None:
        """Clean up stale overlap-hold labels from previous runs."""
        try:
            from kew.overlap import strip_stale_holds

            self._required_failures.extend(strip_stale_holds(self.config))
        except Exception as exc:
            self._required_failures.append(("Strip stale overlap holds", str(exc)))
            _warn(f"Failed to strip stale overlap holds: {exc}")

    @staticmethod
    def _build_overlap_indexes() -> tuple[
        list[str], dict[str, set[str]], dict[str, set[str]]
    ]:
        """Build source file index for overlap prediction.

        Uses ``git ls-files`` (tracked files only) so generated/gitignored
        artifacts like ``.mypy_cache/`` never pollute the overlap index.

        Returns (source_files, module_index, identifier_index); partial
        results are returned when index building fails midway.
        """
        source_files: list[str] = []
        module_index: dict[str, set[str]] = {}
        identifier_index: dict[str, set[str]] = {}
        _SUFFIXES = {".py", ".j2", ".toml", ".yaml", ".yml", ".json", ".md"}
        try:
            from kew.subprocess_utils import run as sp_run

            result = sp_run(["git", "ls-files"], check=True, capture=True)
            source_files = [
                f
                for f in result.stdout.splitlines()
                if any(f.endswith(s) for s in _SUFFIXES)
            ]
            module_index = build_module_index(source_files)
            identifier_index = build_identifier_index(source_files)
        except Exception as exc:
            _warn(f"Failed to build source file index: {exc}")
        return source_files, module_index, identifier_index

    def _dispatch_conflicting_resolves(
        self,
        pool: ProcessPoolExecutor,
        resolve_futures: dict[Future[CommandOutcome], int],
        *,
        max_count: int | None = None,
    ) -> None:
        """Submit resolve workers for open agent PRs with merge conflicts."""
        if not (self.config.resolve.auto and self.has_capacity()):
            return
        for index, pr in enumerate(self._poll_conflicting_prs()):
            if not self.has_capacity() or (
                max_count is not None and index >= max_count
            ):
                break
            pr_num = pr["number"]
            tool_paths = dict(TOOL_PATHS)
            try:
                resolve_fut: Future[CommandOutcome] = pool.submit(
                    _worker_resolve,
                    pr,
                    self.config,
                    tool_paths,
                )
            except Exception as exc:
                self._required_failures.append(
                    (f"Submit resolve PR #{pr_num}", str(exc))
                )
                self._shutdown = True
                _error(f"Failed to submit resolve for PR #{pr_num}: {exc}")
                break
            self.resolve_in_flight.add(pr_num)
            self._resolve_items[pr_num] = self._resolve_item_key(pr)
            _log(f"Dispatching resolve for PR #{pr_num} — {pr.get('title', '')}")
            resolve_futures[resolve_fut] = pr_num

    def _resolve_item_key(self, pr: dict[str, Any]) -> str:
        """The item key a resolve worker's run row will carry for *pr*.

        Mirrors ``dispatch_resolve``: the PR resolves to its issue number
        (store-first, then the PR body and branch), falling back to the PR
        number itself. Used only to match the child against its own pending
        row so it is never counted twice in the fleet capacity.
        """
        from kew.dispatch_core import resolve_issue_number

        try:
            issue = resolve_issue_number(
                pr.get("body") or "", pr.get("headRefName") or "", store=self.store
            )
        except Exception as exc:  # noqa: BLE001 - counting must never abort
            _warn(f"Could not map PR #{pr.get('number')} to its issue: {exc}")
            issue = None
        return str(issue or pr["number"])

    def _apply_overlap_hold(self, num: int, decision: OverlapDecision) -> None:
        """Label the issue as held and register the hold with the coordinator."""
        try:
            _update_labels(
                num,
                self.config,
                add=[self.config.labels.overlap_hold],
            )
            self.coordinator.add_hold(
                num,
                blocking_issues={decision.blocking_issue}
                if decision.blocking_issue
                else set(),
                overlap_files=decision.predicted_files,
            )
        except Exception as exc:
            self._required_failures.append((f"Apply overlap hold #{num}", str(exc)))
            self._shutdown = True
            _warn(f"Failed to hold #{num}: {exc}")

    def _evaluate_overlap(
        self,
        issue: GhIssue,
        *,
        source_files: list[str],
        identifier_index: dict[str, set[str]],
        module_index: dict[str, set[str]],
    ) -> tuple[bool, set[str], str | None]:
        """Run the overlap check for an issue.

        Returns (hold, predicted_files, warning_context). When *hold* is
        True the issue has been held and must not be dispatched.
        """
        if self.config.overlap.strategy == "off":
            return False, set(), None
        num = issue["number"]
        decision = self.coordinator.check(
            issue,
            self.config,
            source_files=source_files,
            identifier_index=identifier_index,
            module_index=module_index,
        )
        if decision.action == "hold":
            _warn(f"Holding #{num}: {decision.reason}")
            self._apply_overlap_hold(num, decision)
            return True, set(), None
        if decision.warning_context:
            _warn(f"Overlap warning for #{num}: {decision.warning_context}")
        return False, decision.predicted_files, decision.warning_context

    def _build_merge_context(self, num: int, warning_context: str | None) -> str | None:
        """Combine the coordinator merge context with any overlap warning."""
        merge_ctx = self.coordinator.get_merge_context(num, self.config)
        if warning_context:
            merge_ctx = (
                f"{warning_context}\n\n{merge_ctx}" if merge_ctx else warning_context
            )
        return merge_ctx

    def _dispatch_issue(
        self,
        issue: GhIssue,
        pool: ProcessPoolExecutor,
        futures: dict[Future[DispatchOutcome], int],
        *,
        source_files: list[str],
        identifier_index: dict[str, set[str]],
        module_index: dict[str, set[str]],
    ) -> None:
        """Check overlap/claim guards for one issue and submit it to the pool."""
        num = issue["number"]
        title = issue.get("title", "")

        hold, predicted_files, warning_context = self._evaluate_overlap(
            issue,
            source_files=source_files,
            identifier_index=identifier_index,
            module_index=module_index,
        )
        if hold:
            return

        # Guard against duplicate dispatch. The branch derives from the
        # item-id slug (A5a) so it matches ``execute_dispatch``'s branch build;
        # GitHub numbers slug to themselves, keeping the name unchanged.
        branch_name = f"{self.config.branches.prefix}{branch_slug(str(num))}"
        if remote_branch_exists(branch_name):
            _warn(f"Skipping #{num} — remote branch '{branch_name}' already exists")
            self._record_skip(num)
            return

        claim = try_claim_issue(num, self.config)
        if not claim:
            if claim is ClaimResult.NOT_READY:
                _warn(f"Skipping #{num}: not labeled '{self.config.labels.ready}'.")
            elif claim is ClaimResult.ALREADY_CLAIMED:
                _warn(f"Skipping #{num}: another dispatch already claimed it.")
            else:  # The ready label may already have been removed.
                self._claim_failures.append((num, claim))
                # Finish this batch, then drain without selecting this issue again.
                self._shutdown = True
                _warn(f"Skipping #{num}: claim failed ({claim.value}).")
            return

        from kew.dispatch_core import _warn_if_cap_conflict

        _warn_if_cap_conflict(self.config, self.store, num, str(num))

        tool_paths = dict(TOOL_PATHS)
        merge_ctx = self._build_merge_context(num, warning_context)
        try:
            dispatch_fut: Future[DispatchOutcome] = pool.submit(
                _worker_dispatch,
                issue,
                self.config,
                tool_paths,
                merge_ctx,
            )
        except Exception as exc:
            self._required_failures.append((f"Submit issue #{num}", str(exc)))
            self._shutdown = True
            _error(f"Failed to submit #{num} to pool: {exc}")
            self._rollback_claim(num)
            return

        self.in_flight.add(num)
        self.coordinator.on_dispatch(num, predicted_files)
        _log(f"Dispatching #{num} — {title}")
        futures[dispatch_fut] = num

    def _rollback_claim(self, num: int) -> None:
        """Undo a claim whose pool.submit failed: restore ``ready`` + audit.

        Best-effort throughout — this runs on an error path and must not raise.
        Without it the issue stays in-progress with no worker until
        ``kew cleanup`` repairs it (#272).
        """
        try:
            rollback_claim(num, self.config)
        except Exception as exc:
            self._required_failures.append((f"Rollback claim #{num}", str(exc)))
            _error(f"Failed to restore 'ready' for #{num} after submit failure: {exc}")

        try:
            with (
                nullcontext(self.store)
                if self.store is not None
                else _open_scheduler_store(self.config)
            ) as store:
                audit.emit(
                    store,
                    "claim_rollback",
                    actor=Actor("policy", "pool-submit-failed"),
                    issue_num=num,
                    repo=self.config.project.repo or None,
                    payload={"reason": "pool_submit_failed"},
                )
        except Exception as exc:
            self._required_failures.append((f"Record claim rollback #{num}", str(exc)))
            _warn(f"Failed to record claim_rollback audit event for #{num}: {exc}")

    def _dispatch_batch(
        self,
        issues: list[GhIssue],
        pool: ProcessPoolExecutor,
        futures: dict[Future[DispatchOutcome], int],
        *,
        show_prompt: bool,
        dry_run: bool,
        source_files: list[str],
        identifier_index: dict[str, set[str]],
        module_index: dict[str, set[str]],
    ) -> None:
        """Preview or dispatch each selected issue in turn."""
        for issue in issues:
            if self._handle_preview(issue, show_prompt, dry_run):
                continue
            self._dispatch_issue(
                issue,
                pool,
                futures,
                source_files=source_files,
                identifier_index=identifier_index,
                module_index=module_index,
            )

    def _reap_loop_workers(
        self,
        futures: dict[Future[DispatchOutcome], int],
        resolve_futures: dict[Future[CommandOutcome], int],
    ) -> bool:
        """Collect child outcomes; return whether a child refused over capacity.

        The caller waits the poll interval for a refusal only after the
        fleet-stop latch and breaker checks, so no capacity wait precedes them.
        """
        over_cap = self._reap_completed(futures) is True
        self._reap_resolve_completed(resolve_futures)
        return over_cap

    def _fleet_stop_requested(self) -> bool:
        """Check the fleet-stop latch and the breaker at the top of every pass.

        Runs before any capacity wait so a full fleet still honors ``kew stop``
        and a breaker trip within one pass, exactly as the loop did before the
        durable capacity gate existed.
        """
        if self._fleet_latched():
            _warn(
                "Fleet stopped (`kew stop`) — draining workers"
                " and exiting. Run `kew resume` to re-enable dispatch."
            )
            return True
        # A breaker trip must stop new claims in this same pass (#245).
        self._check_fleet_breaker()
        return self._fleet_latched()

    def _poll_dispatch_slots(
        self,
        pool: ProcessPoolExecutor,
        resolve_futures: dict[Future[CommandOutcome], int],
        *,
        preview: bool = False,
    ) -> int | None:
        """Read durable capacity before submitting any new workers.

        Called only once local capacity exists. A full durable count waits the
        60s poll interval here so the caller can ``continue`` without touching
        the empty-queue streak; after ``_CAPACITY_WAIT_CHECKS`` consecutive
        such waits the loop exits (``None``) naming the holders, so a fleet
        held full by other actors never idles this process until the 24-hour
        backstop. The exit fires only while this parent owns no running
        child: its own children finish on their own timeouts and free slots,
        and quitting mid-work would abandon them. A preview (``--dry-run`` or
        ``--show-prompt``) claims no worker slot and must reach selection and
        exit after showing the batch, so it never waits: it keeps the durable
        count while slots remain and falls back to the local count only when
        the durable fleet is full.
        """
        slots = self._fleet_available_slots(log_holders=not preview)
        if slots > 0:
            self._durable_full_checks = 0
            before = len(resolve_futures)
            self._dispatch_conflicting_resolves(pool, resolve_futures, max_count=slots)
            if self._shutdown:
                return None
            return max(0, slots - (len(resolve_futures) - before))
        if preview:
            return None if self._capacity_read_failed else max(1, self.available_slots)
        return self._wait_for_fleet_capacity()

    def _wait_for_fleet_capacity(self) -> int | None:
        """Bound capacity retries while allowing active workers to finish."""
        self._durable_full_checks += 1
        owns_children = bool(self.in_flight or self.resolve_in_flight)
        if (
            self._durable_full_checks >= self._CAPACITY_WAIT_CHECKS
            and not owns_children
        ):
            holders = ", ".join(
                f"{item} ({verdict})"
                for item, verdict in self._last_capacity_holders or ()
            )
            state = "unavailable" if self._capacity_read_failed else "full"
            _log(
                f"Fleet capacity {state} for {self._durable_full_checks} checks "
                f"(held by: {holders or 'none'}). Exiting; rerun when a slot frees."
            )
            return None
        time.sleep(60)
        return 0

    def _slots_for_pass(
        self,
        pool: ProcessPoolExecutor,
        futures: dict[Future[DispatchOutcome], int],
        resolve_futures: dict[Future[CommandOutcome], int],
        *,
        preview: bool,
    ) -> int | None:
        """Run one loop pass up to selection and return its slot count.

        ``None`` means the loop must stop (fleet latch, breaker, or the
        bounded durable-capacity wait ran out); ``0`` means the pass ended in
        a wait (an over-cap refusal, local capacity, or the durable fleet) and
        the caller continues without touching the empty-queue streak.
        """
        self._pending_liveness = None
        over_cap = self._reap_loop_workers(futures, resolve_futures)
        # The age-gated sweep runs on the first pass even when the fleet is
        # latched (the pre-loop sweep's contract) and then at most once per
        # _STALE_PENDING_SWEEP_SECONDS, so a row that goes stale after the
        # loop starts releases its slot without a restart.
        self._next_stale_pending_sweep = self._cleanup_stale_pending_if_due(
            self._next_stale_pending_sweep
        )
        # Fleet kill switch and breaker run before any capacity wait.
        if self._fleet_stop_requested():
            return None
        if over_cap:
            # A child refused over capacity: wait the poll interval before
            # re-selecting so the admit/refuse cycle cannot hot-loop. No skip
            # is recorded for the issue.
            time.sleep(60)
            return 0
        if not self.has_capacity():
            time.sleep(2)
            return 0
        return self._poll_dispatch_slots(pool, resolve_futures, preview=preview)

    def run_loop(
        self,
        *,
        force_issue: int | None = None,
        dry_run: bool = False,
        show_prompt: bool = False,
    ) -> DrainResult:
        """Main scheduler loop using ProcessPoolExecutor."""
        self._claim_failures.clear()
        self._dispatch_outcomes.clear()
        self._resolve_outcomes.clear()
        self._required_failures.clear()
        prev_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        futures: dict[Future[DispatchOutcome], int] = {}
        resolve_futures: dict[Future[CommandOutcome], int] = {}
        consecutive_empty = 0
        pool = ProcessPoolExecutor(max_workers=self.max_workers)

        source_files: list[str] = []
        module_index: dict[str, set[str]] = {}
        identifier_index: dict[str, set[str]] = {}
        if self.config.overlap.strategy != "off":
            self._strip_stale_overlap_holds()
            source_files, module_index, identifier_index = self._build_overlap_indexes()

        self._next_stale_pending_sweep = 0.0
        self._empty_teaching_shown = False
        with self._drain_on_exit(pool, futures, resolve_futures, prev_handler):
            while not self._shutdown:
                slots = self._slots_for_pass(
                    pool, futures, resolve_futures, preview=show_prompt or dry_run
                )
                if slots is None:
                    break
                if slots <= 0:
                    continue

                issues = self.select_available_issues(
                    max_count=slots, force_issue=force_issue
                )

                if not issues:
                    consecutive_empty = self._handle_no_selection(
                        consecutive_empty, force_issue
                    )
                    if consecutive_empty >= 3:
                        break
                    continue

                consecutive_empty = 0
                self._empty_teaching_shown = False

                self._dispatch_batch(
                    issues,
                    pool,
                    futures,
                    show_prompt=show_prompt,
                    dry_run=dry_run,
                    source_files=source_files,
                    identifier_index=identifier_index,
                    module_index=module_index,
                )

                # If in preview mode, exit after showing all issues
                if show_prompt or dry_run:
                    break

        return DrainResult(
            tuple(self._claim_failures),
            tuple(self._dispatch_outcomes),
            tuple(self._resolve_outcomes),
            tuple(self._required_failures),
        )

    @contextmanager
    def _drain_on_exit(
        self,
        pool: ProcessPoolExecutor,
        futures: dict[Future[DispatchOutcome], int],
        resolve_futures: dict[Future[CommandOutcome], int],
        prev_handler: Any,
    ) -> Iterator[None]:
        """Retain selection failures and drain both collections before shutdown."""
        try:
            yield
        except GhQueryError as exc:
            self._required_failures.append(("Selection query", str(exc)))
            _error(f"Selection query failed: {exc}")
        finally:
            try:
                self._wait_for_completion(futures)
            finally:
                try:
                    self._wait_for_resolve_completion(resolve_futures)
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
                    signal.signal(signal.SIGINT, prev_handler)
                    self._report_claim_failures()

    def _report_claim_failures(self) -> None:
        """Name claims whose labels may need operator inspection."""
        for number, claim in self._claim_failures:
            _warn(f"Potentially stranded claim: issue #{number} ({claim.value}).")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reap_completed(self, futures: dict[Future[DispatchOutcome], int]) -> bool:
        """Remove completed children and report whether any refused capacity."""
        requeued_over_cap = False
        done = [f for f in futures if f.done()]
        for fut in done:
            issue_num = futures.pop(fut)
            self.in_flight.discard(issue_num)
            try:
                outcome = fut.result()
                if self._record_outcome(issue_num, outcome):
                    requeued_over_cap = True
                # Notify coordinator of completion
                released = self.coordinator.on_complete(
                    issue_num, outcome.changed_files
                )
                for released_num in released:
                    try:
                        _update_labels(
                            released_num,
                            self.config,
                            remove=[self.config.labels.overlap_hold],
                        )
                        _log(f"Released overlap hold on #{released_num}")
                    except Exception as exc:
                        self._required_failures.append(
                            (f"Release overlap hold #{released_num}", str(exc))
                        )
                        _warn(f"Failed to release hold on #{released_num}: {exc}")
                        # Re-add hold so it can be retried or cleaned up
                        self.coordinator.add_hold(
                            released_num,
                            blocking_issues=set(),
                            overlap_files=set(),
                        )
                message = (
                    f"Worker #{issue_num} exited with code {outcome.exit_code}: "
                    f"{outcome.category} — {outcome.reason}"
                )
                if outcome.exit_code == 0:
                    _success(message)
                else:
                    _warn(message)
            except Exception as exc:
                self._required_failures.append((f"Issue #{issue_num}", str(exc)))
                self.coordinator.on_complete(issue_num, [])
                _error(f"Worker #{issue_num} raised exception: {exc}")
                mark_failed(issue_num, self.config, f"exception: {exc}")
        return requeued_over_cap

    def _record_outcome(self, issue_num: int, outcome: DispatchOutcome) -> bool:
        """Retain one worker outcome; report whether it refused capacity."""
        self._dispatch_outcomes.append((issue_num, outcome))
        if outcome.category == CLAIM_ROLLBACK_FAILED:
            # The refusal could not hand the claim back: the issue stays
            # claimed with no run and later drains skip it. Name it, and stop
            # claiming more issues while their labels cannot be restored.
            self._required_failures.append(
                (f"Rollback claim #{issue_num}", outcome.reason)
            )
            self._shutdown = True
        if outcome.category == PROMPT_CONFIGURATION_REFUSAL:
            # The issue went back to ready and every other one would
            # be refused the same way: stop selecting until kew.toml
            # or the profile changes.
            self._shutdown = True
        return outcome.category == "requeued_over_cap"

    def _wait_for_completion(self, futures: dict[Future[DispatchOutcome], int]) -> None:
        """Wait for remaining futures with grace period."""
        if not futures:
            return
        grace = self.config.parallel.shutdown_grace_seconds
        _log(f"Waiting up to {grace}s for {len(futures)} worker(s) to finish...")
        deadline = time.monotonic() + grace
        while futures and time.monotonic() < deadline:
            self._reap_completed(futures)
            self._check_fleet_breaker()
            if futures:
                time.sleep(1)
        if futures:
            self._reap_completed(futures)
        if futures:
            remaining = list(futures.values())
            self._required_failures.extend(
                (f"Issue #{number}", "worker exceeded shutdown grace")
                for number in remaining
            )
            _warn(f"Timed out waiting for workers: {remaining}")

    def _wait_for_resolve_completion(
        self, futures: dict[Future[CommandOutcome], int]
    ) -> None:
        """Wait for remaining resolve futures with grace period."""
        if not futures:
            return
        grace = self.config.parallel.shutdown_grace_seconds
        _log(
            f"Waiting up to {grace}s for {len(futures)} resolve worker(s) to finish..."
        )
        deadline = time.monotonic() + grace
        while futures and time.monotonic() < deadline:
            self._reap_resolve_completed(futures)
            if futures:
                time.sleep(1)
        if futures:
            self._reap_resolve_completed(futures)
        if futures:
            remaining = list(futures.values())
            self._required_failures.extend(
                (f"Resolve PR #{number}", "worker exceeded shutdown grace")
                for number in remaining
            )
            _warn(f"Timed out waiting for resolve workers: {remaining}")

    def _handle_shutdown(self, signum: int, frame: types.FrameType | None) -> None:
        """SIGINT handler — request graceful shutdown."""
        _warn("Shutdown requested, finishing in-flight workers...")
        self._shutdown = True

    def _fleet_latched(self) -> bool:
        """Return True when the fleet-stop latch (`kew stop`) is engaged."""
        db_path = Path(self.config.logging.dir).resolve() / "dispatch.db"
        if self.store is None and not db_path.exists():
            return False
        try:
            with (
                nullcontext(self.store)
                if self.store is not None
                else _open_scheduler_store(self.config)
            ) as store:
                return store.get_fleet_stopped() is not None
        except Exception as exc:
            _warn(f"Failed to check fleet latch: {exc}")
            return False

    @staticmethod
    def _fleet_breaker_sums(recent: list[Any], day_start: Any) -> tuple[float, float]:
        """Compute daily and weekly spend from a list of RunRecords."""
        daily: float = sum(
            r.estimated_cost_usd for r in recent if r.started_at >= day_start
        )
        weekly: float = sum(r.estimated_cost_usd for r in recent)
        return daily, weekly

    def _check_fleet_breaker(self) -> None:
        """Engage the fleet stop when persisted daily/weekly spend breaches the
        configured limit (#245). No-op when the breaker is disabled, the latch
        is already set, or spend is within budget. Best-effort: never raises."""
        if not self.config.breaker.enabled:
            return
        from datetime import UTC, datetime, timedelta

        from kew.breaker import evaluate_fleet

        db_path = Path(self.config.logging.dir).resolve() / "dispatch.db"
        if self.store is None and not db_path.exists():
            return
        try:
            now = datetime.now(UTC)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start = day_start - timedelta(days=6)
            with (
                nullcontext(self.store)
                if self.store is not None
                else _open_scheduler_store(self.config)
            ) as store:
                if store.get_fleet_stopped() is not None:
                    return
                recent = store.list_since(week_start)
                daily, weekly = self._fleet_breaker_sums(recent, day_start)
                trip = evaluate_fleet(
                    daily,
                    self.config.budget.daily_usd,
                    weekly,
                    self.config.budget.weekly_usd,
                )
                if trip is None:
                    return
                store.set_fleet_stopped(trip.detail)
                from kew.audit import Actor, emit

                emit(
                    store,
                    "circuit_breaker",
                    actor=Actor("policy", trip.rule),
                    repo=self.config.project.repo or None,
                    payload={
                        "rule": trip.rule,
                        "scope": "fleet",
                        "observed_usd": round(trip.observed_usd or 0.0, 4),
                        "threshold_usd": trip.threshold_usd,
                    },
                )
            _error(f"Circuit breaker engaged fleet stop: {trip.detail}")
        except Exception as exc:
            _warn(f"Fleet breaker check failed: {exc}")

    def _handle_preview(
        self,
        issue: GhIssue,
        show_prompt: bool,
        dry_run: bool,
    ) -> bool:
        """Handle dry-run / show-prompt mode. Returns True if handled."""
        num = issue.get("number", 0)
        title = issue.get("title", "")

        if dry_run:
            _success(f"[DRY RUN] Would dispatch: #{num} — {title}")
            return True

        if show_prompt:
            issue_labels = issue.get("labels", [])
            complexity = get_complexity(issue_labels)
            item_id = str(num)
            preview_route = resolve_runner(self.config, complexity=complexity)
            prompt = build_prompt(
                issue_num=int(num),
                item_id=item_id,
                issue_title=str(title),
                issue_body=str(issue.get("body") or ""),
                complexity=complexity,
                config=self.config,
                has_coderabbit="coderabbit" in TOOL_PATHS,
                publication_style=preview_route.runner.publication_style,
                profile_addendum=launch_addendum(self.store),
            )
            import typer

            typer.echo(prompt)
            return True

        return False


def _worker_dispatch(
    issue: GhIssue,
    config: DispatchConfig,
    tool_paths: dict[str, str],
    merge_context: str | None = None,
) -> DispatchOutcome:
    """Worker function that runs in a subprocess pool.

    Restores TOOL_PATHS then delegates to execute_dispatch().
    """
    from kew.subprocess_utils import TOOL_PATHS as _TOOL_PATHS

    _TOOL_PATHS.update(tool_paths)

    from kew.dispatch_core import execute_dispatch

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    with _open_scheduler_store(config) as store:
        outcome = execute_dispatch(
            issue=issue,
            config=config,
            store=store,
            merge_context=merge_context,
            fleet_worker=True,
        )
        return outcome


def _worker_resolve(
    pr: dict[str, Any],
    config: DispatchConfig,
    tool_paths: dict[str, str],
) -> CommandOutcome:
    """Worker function for resolve tasks in the subprocess pool.

    Restores TOOL_PATHS then delegates to dispatch_resolve().
    """
    from kew.subprocess_utils import TOOL_PATHS as _TOOL_PATHS

    _TOOL_PATHS.update(tool_paths)

    from kew.resolve import dispatch_resolve

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    with _open_scheduler_store(config) as store:
        return dispatch_resolve(
            pr_number=pr["number"],
            config=config,
            store=store,
        )
