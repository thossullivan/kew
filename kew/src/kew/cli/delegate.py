"""`kew delegate` command group: the foreground assign-to-kew watch loop (D5).

``kew delegate watch`` assembles the D3 watcher → D2 recognition → D4 claim/
dispatch seam into one foreground loop (like ``kew ingest serve`` — no daemon,
no autostart, respects #322/#323). Delegated work shares the ``epic run --all``
fleet-cap ceiling (``[parallel] max_workers``), so delegated and epic
reservations can never exceed one combined worker count. ``--dry-run`` logs the
intent without reserving or spending; Ctrl-C shuts down cleanly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from kew import cli as _c
from kew.cli._apps import delegate_app
from kew.config import resolved_config_path
from kew.delegate.claim import DelegationTracker
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import (
    make_is_terminally_decided,
    process_candidate,
    reconcile_delegation_reservations,
    reconcile_stranded_rollbacks,
    run_watch_loop,
)
from kew.graph.driver.credentials import driver_key_cleanup, reconcile_driver_keys
from kew.graph.driver.dispatch import reap_dispatch_children
from kew.progress import _error, _log, _success
from kew.pseudo_epics import DELEGATION_EPIC_REF

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.delegate.watch import DelegationWatcher
    from kew.graph.driver.dispatch import DispatchSink
    from kew.linear.client import LinearClient
    from kew.linear.tracker import LinearTracker
    from kew.run_store import RunStore

# The live-state re-verify query for the delegation tracker: exactly the three
# fields recognition (D2) and the claim re-verify (D4) read — the current
# assignee id, workflow-state name, project membership, and label names.
_LIVE_FETCH_QUERY = """
query DelegationLive($id: String!) {
  issue(id: $id) {
    identifier
    state { id name type }
    project { id }
    assignee { id }
    labels { nodes { name } }
  }
}
"""


class _LinearDelegationTracker:
    """Concrete :class:`DelegationTracker` over the live Linear API.

    ``fetch_live`` runs a tight three-field query so recognition and the D4
    claim re-verify read the issue's LIVE assignee/state/labels (never a
    snapshot). ``transition_in_progress`` and ``comment`` reuse the existing
    :class:`~kew.linear.tracker.LinearTracker` write vertical rather than
    reimplementing the state-id resolution or the comment mutation.
    """

    def __init__(self, client: LinearClient, tracker: LinearTracker) -> None:
        self._client = client
        self._tracker = tracker

    def _fetch_issue(self, item_id: str) -> Any:
        """Run the live query, mapping the Linear taxonomy onto the delegate-level
        classes the watch loop handles (N3): a not-found item is permanently gone;
        a gateway blip or an exhausted rate-limit is transient. An auth failure is
        FATAL (Task 3, #971) — an expired/revoked key is never fixed by retrying,
        so it must terminate the loop, not be deferred forever. Any other hard API
        error stays transient so the loop backs off rather than crash-looping."""
        from kew.delegate.errors import (
            FatalTrackerError,
            ItemGone,
            TransientTrackerError,
        )
        from kew.linear.client import (
            LinearApiError,
            LinearAuthError,
            LinearUnavailableError,
        )

        try:
            issue = self._client.execute(_LIVE_FETCH_QUERY, {"id": item_id})["issue"]
        except LinearAuthError as exc:
            raise FatalTrackerError(str(exc)) from exc
        except LinearUnavailableError as exc:
            raise TransientTrackerError(str(exc)) from exc
        except LinearApiError as exc:
            if exc.not_found:
                raise ItemGone(str(exc)) from exc
            raise TransientTrackerError(str(exc)) from exc
        if issue is None:
            raise ItemGone(f"Linear issue {item_id} not found.")
        return issue

    def fetch_live(self, item_id: str) -> CandidateIssue:
        issue = self._fetch_issue(item_id)
        assignee = issue.get("assignee") or {}
        state = issue.get("state") or {}
        label_nodes = (issue.get("labels") or {}).get("nodes") or []
        labels = tuple(n["name"] for n in label_nodes if n.get("name"))
        from kew.graph.adapters.linear_native import is_linear_epic_admitted

        return CandidateIssue(
            assignee_id=assignee.get("id"),
            labels=labels,
            state=state.get("name") or "",
            state_id=state.get("id") or "",
            epic_admitted=is_linear_epic_admitted(
                state,
                frozenset(labels),
                project_id=(issue.get("project") or {}).get("id"),
            ),
        )

    def transition_in_progress(self, item_id: str) -> None:
        # LinearTracker.claim assigns the viewer (the kew seat) AND moves the
        # issue to the started (In Progress) state — the issue is already
        # assigned to kew here, so the assignment is a harmless re-assert.
        self._tracker.claim(item_id)

    def comment(self, item_id: str, body: str) -> None:
        self._tracker.comment(item_id, body)


class _FatalMappingPoll:
    """Wraps the poll floor so a fatal auth error surfaces to the watch loop.

    The watcher isolates a transient poll failure (down tunnel / rate limit) by
    swallowing it, but an expired/revoked key must NOT be swallowed and retried
    forever (Task 3, #971): this maps a ``LinearAuthError`` from the poll leg onto
    the delegate-level ``FatalTrackerError`` the loop treats as terminal. Every
    other error passes through unchanged for the watcher to isolate as before.
    """

    def __init__(self, poll: Any) -> None:
        self._poll = poll

    def list_delegated(self, assignee_id: str, label: str) -> list[str]:
        from kew.delegate.errors import FatalTrackerError
        from kew.linear.client import LinearAuthError

        try:
            return self._poll.list_delegated(assignee_id, label)  # type: ignore[no-any-return]
        except LinearAuthError as exc:
            raise FatalTrackerError(str(exc)) from exc


@dataclass
class _WatchContext:
    """The live-wired pieces the watch loop drives, built once at startup."""

    watcher: DelegationWatcher
    tracker: DelegationTracker
    sink: DispatchSink
    member_id: str


def _build_watch_context(config: Any, store: RunStore) -> _WatchContext:
    """Resolve KEW_MEMBER_ID and wire the live Linear watcher/tracker/sink.

    Kept as a seam the CLI test patches, so ``watch`` itself stays thin and the
    loop can be driven with fakes without touching the Linear API.
    """
    import os

    from kew.delegate.recognize import MemberIdResolver
    from kew.delegate.watch import DelegationWatcher, MetaCursorStore
    from kew.graph.adapters.linear_native import LinearSource
    from kew.graph.driver.dispatch import DetachedDispatchSink
    from kew.linear.client import LinearClient
    from kew.linear.tracker import LinearTracker

    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        _error(
            "tracker.type is 'linear' but the LINEAR_API_KEY environment "
            "variable is not set."
        )
        raise typer.Exit(2)

    client = LinearClient(api_key)
    source = LinearSource(client, in_review_state=config.tracker.linear.in_review_state)
    member_id = MemberIdResolver(source, config.delegation.assignee).resolve()
    # Resolve the trigger label's UUID once at startup so the fast local read can
    # decide human-assignee churn on a labeled issue locally (N4). None (missing
    # or ambiguous name) keeps the conservative assignee-only local filter.
    label_id = source.resolve_label_id(config.delegation.label)
    tracker = _LinearDelegationTracker(client, LinearTracker(config, client))
    sink = DetachedDispatchSink(
        epic_ref=DELEGATION_EPIC_REF, run_kind="delegation", config=config, store=store
    )
    watcher = DelegationWatcher(
        events=store,
        # Map a fatal auth error from the poll leg onto FatalTrackerError so the
        # watch loop terminates instead of swallowing it forever (Task 3, #971).
        poll=_FatalMappingPoll(source),
        cursor_store=MetaCursorStore(store),
        assignee_id=member_id,
        label=config.delegation.label,
        is_terminally_decided=make_is_terminally_decided(store),
        poll_interval_seconds=config.delegation.poll_interval_seconds,
        clock=time.monotonic,
        label_id=label_id,
    )
    return _WatchContext(
        watcher=watcher, tracker=tracker, sink=sink, member_id=member_id
    )


def _reconcile_watch(
    store: RunStore,
    config: DispatchConfig,
    context: _WatchContext,
    revoked_hashes: set[str],
) -> None:
    """Reap dispatch children immediately before the reservation sweep."""
    from datetime import UTC, datetime

    reap_dispatch_children(context.sink)
    # Issued run keys are reconciled first: the pseudo-epic has no driver to
    # sweep them, so a revocation left pending would outlive the run.
    revoked_hashes.clear()
    reconcile_driver_keys(store, now=datetime.now(UTC), seen=revoked_hashes)
    reconcile_delegation_reservations(store, config, now=datetime.now(UTC))
    # Repair items stranded In Progress by a failed refusal rollback so a
    # rate-limited reset_to_ready leaves a retry surface (Task 2, #971).
    reconcile_stranded_rollbacks(store, context.tracker)


@delegate_app.command("watch")
def watch(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Log 'would claim / would dispatch' without reserving or spending.",
        ),
    ] = False,
) -> None:
    """Watch for assign-to-kew delegation and claim → dispatch it (foreground)."""
    config = _c.load_config()
    if config.tracker.type != "linear":
        _error(
            "kew delegate watch requires tracker.type == 'linear' "
            f"(this project is configured with '{config.tracker.type}')."
        )
        raise typer.Exit(2)
    if not config.delegation.assignee:
        _error(
            "set [delegation].assignee (the kew Linear member name) before "
            "running kew delegate watch."
        )
        raise typer.Exit(2)

    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        _c._open_run_store(str(db_path)) as store,
        driver_key_cleanup(store) as revoked_hashes,
    ):
        # Re-resolve with the store so DB overrides govern the budget preflight
        # AND the shared fleet cap (R1, #902) — the file-only config above would
        # refuse or admit on a stale value.
        config = _c.load_config(store=store)
        context = _build_watch_context(config, store)
        fleet_cap = config.fleet_concurrency()

        _log(
            f"kew delegate watch: assignee={config.delegation.assignee!r} "
            f"(member {context.member_id}) label={config.delegation.label!r} "
            f"fast={config.delegation.fast_interval_seconds}s "
            f"poll={config.delegation.poll_interval_seconds}s "
            f"fleet_cap={fleet_cap}" + (" [DRY RUN]" if dry_run else "")
        )

        config_path = resolved_config_path()

        # Bounded backoff on a transient tracker error (N3): a rate-limit / blip
        # sleeps one fast interval (capped) so the loop doesn't hammer a
        # recovering tracker, without stalling the foreground watch for long.
        backoff_s = min(config.delegation.fast_interval_seconds, 5.0)

        def _backoff() -> None:
            time.sleep(backoff_s)

        def _process(candidate: Any) -> Any:
            return process_candidate(
                candidate,
                member_id=context.member_id,
                label=config.delegation.label,
                store=store,
                tracker=context.tracker,
                sink=context.sink,
                fleet_cap=fleet_cap,
                dry_run=dry_run,
                config_path=config_path,
                backoff=_backoff,
                refusal_cooldown_seconds=config.delegation.refusal_cooldown_seconds,
            )

        _success("kew delegate watch: watching — Ctrl-C to stop")
        try:
            run_watch_loop(
                watcher=context.watcher,
                process_one=_process,
                sleep=time.sleep,
                fast_interval_seconds=config.delegation.fast_interval_seconds,
                reconcile=partial(
                    _reconcile_watch, store, config, context, revoked_hashes
                ),
                reap=partial(reap_dispatch_children, context.sink),
            )
        except KeyboardInterrupt:
            _success("kew delegate watch: stopped")
