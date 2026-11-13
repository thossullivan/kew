"""Aggregate the console's judgment-queue payload (#700, Group 1).

``assemble(store, config)`` is a pure aggregator: it returns one
:class:`DashboardDTO` built from shipped readers — the needs-you set, fleet-stop
state, spend windows, routing, and the ready queue. Every source is fail-soft: a
reader that raises degrades to an ``unavailable`` marker and its ``as_of`` entry
is dropped, so one flaky source never 500s the whole payload (the endpoint stays
a thin shim).

``rows`` is NOT composed here. It is the needs-you set, composed once in
:mod:`kew.service.needs_you` and served verbatim, because the factory header's
count, Monitor's holds column and the Work screen's needs-you filter all read
this one field and may never disagree about what the words mean (#2207). This
module's job is the glance beside it and the banner above it.

Layering: ``store`` is duck-typed (never imports ``run_store`` types), and the
readers imported here all live in ``kew.report`` / ``kew.service`` — the serve
layer reaches this through the service boundary only.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.config import resolved_config_path
from kew.drive_heartbeat import read_drive_heartbeat
from kew.epic_archive import archived_epic_refs
from kew.service.attention import rec_item_id
from kew.service.epics import driver_liveness
from kew.service.fleet import fleet_status
from kew.service.models import (
    CompletionDTO,
    DashboardBannerDTO,
    DashboardDTO,
    DashboardGlanceDTO,
    QueueCandidateDTO,
    ReadyIssueDTO,
    RouteRowDTO,
)
from kew.service.needs_you import (
    needs_you_by_epic,
    needs_you_set,
    needs_you_without_epic,
)
from kew.service.report import governed_spend_windows
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.config import DispatchConfig, RouteEntry

_RECENT_COMPLETIONS = 5

# The ready queue is the only per-request gh round-trip; the dashboard polls
# every 5-10s, so a short module-level TTL collapses many clients' polls into
# one gh call. Keyed by repo. A failure re-raises so the source() wrapper marks
# the queue unavailable, and it is held for _FAILURE_BACKOFF_S (see
# _cached_gh_read).
_READY_CACHE_TTL_S = 30.0
_ready_cache: dict[str, tuple[float, list[ReadyIssueDTO]]] = {}

# The queue candidates are the other per-request gh read: every open issue
# (`--limit 1000`, paged, so several GraphQL points a call). Uncached, one open
# console tab polling every few seconds spent thousands of points an hour and,
# with the outcome sweep, drained the account's quota (#2264). The list
# changes when someone files or labels an issue, so a minute is fresh enough.
# Same contract as the ready cache.
_CANDIDATES_CACHE_TTL_S = 60.0
_candidates_cache: dict[str, tuple[float, list[QueueCandidateDTO]]] = {}

# The failure path (design review c1, C6). With failures uncached and no
# timeout, a rate-limited or hung gh turned every dashboard poll into two new
# gh calls (about 17 a minute per open tab) and each hung call held a server
# worker thread for good: the #2264 drain, back through the error path. Now
# every gh read has a timeout, a failed read is answered from memory for
# _FAILURE_BACKOFF_S before gh is tried again, and one refill runs at a time
# per read and repo (the others wait for it and take its answer).
_GH_TIMEOUT_S = 20.0
_FAILURE_BACKOFF_S = 30.0
_read_failures: dict[tuple[str, str], tuple[float, Exception]] = {}
_read_locks: dict[tuple[str, str], threading.Lock] = {}
_read_locks_guard = threading.Lock()

# Stale-while-revalidate (design review c4, K8). The refill used to run on the
# request path: once an entry expired, the next poll waited on up to two gh
# reads of up to _GH_TIMEOUT_S each, and the console's 7s pulse froze behind
# it. Now only the very first read of a (kind, repo) blocks. An expired entry
# is served as it stands while one background thread refills it; a refill that
# fails is held like any failure, so the field reads unavailable for the
# backoff rather than passing an old answer off as a healthy one forever.
_REFRESH_THREAD_NAME = "kew-dashboard-refresh"


def _refill[T](
    key: tuple[str, str],
    repo: str,
    cache: dict[str, tuple[float, T]],
    read: Callable[[], T],
) -> T:
    """Run ``read()`` and record its outcome — the value in ``cache``, or the
    failure in ``_read_failures``. The caller holds the (kind, repo) lock."""
    started = time.monotonic()
    try:
        value = read()
    except Exception as exc:
        _read_failures[key] = (started, exc)
        raise
    _read_failures.pop(key, None)
    cache[repo] = (time.monotonic(), value)
    return value


def _refresh_in_background[T](
    key: tuple[str, str],
    repo: str,
    cache: dict[str, tuple[float, T]],
    lock: threading.Lock,
    read: Callable[[], T],
) -> None:
    """Refill ``cache`` off the request path, at most once at a time per key:
    a refresh already out (or a cold read holding the lock) is left to land,
    and this request takes the value it already has."""
    if not lock.acquire(blocking=False):
        return

    def refresh() -> None:
        # The failure is not lost: _refill records it in _read_failures, and the
        # next request reads it from there.
        try:
            with contextlib.suppress(Exception):
                _refill(key, repo, cache, read)
        finally:
            lock.release()

    try:
        threading.Thread(target=refresh, name=_REFRESH_THREAD_NAME, daemon=True).start()
    except Exception:
        lock.release()
        raise


def _cached_gh_read[T](
    kind: str,
    repo: str,
    cache: dict[str, tuple[float, T]],
    ttl_s: float,
    read: Callable[[], T],
) -> T:
    """``read()`` behind ``cache`` (``ttl_s``), the failure backoff, and a
    per-(kind, repo) single-flight lock. A held failure re-raises as a
    RuntimeError naming the original error, so ``source()`` still marks the
    field unavailable; it is never served as an empty, healthy answer.

    Only a cold read (nothing cached yet) runs ``read()`` on the caller's
    thread. An expired entry is returned at once while a background thread
    refills it (see ``_REFRESH_THREAD_NAME``)."""
    hit = cache.get(repo)
    if hit is not None and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    key = (kind, repo)
    with _read_locks_guard:
        lock = _read_locks.setdefault(key, threading.Lock())
    failed = _read_failures.get(key)
    if failed is not None and time.monotonic() - failed[0] < _FAILURE_BACKOFF_S:
        raise RuntimeError(f"{kind} read failed recently; retrying later: {failed[1]}")
    if hit is not None:
        _refresh_in_background(key, repo, cache, lock, read)
        return hit[1]
    with lock:
        now = time.monotonic()
        hit = cache.get(repo)
        if hit is not None and now - hit[0] < ttl_s:
            return hit[1]
        failed = _read_failures.get(key)
        if failed is not None and now - failed[0] < _FAILURE_BACKOFF_S:
            raise RuntimeError(
                f"{kind} read failed recently; retrying later: {failed[1]}"
            )
        return _refill(key, repo, cache, read)


def _iso(value: Any) -> str:
    """Best-effort ISO-8601 string from a datetime | str | None ("" for None)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def assemble(
    store: Any, config: DispatchConfig, *, now: datetime | None = None
) -> DashboardDTO:
    """Assemble the judgment-queue payload from shipped readers (fail-soft).

    ``rows`` is the needs-you set, verbatim. Composing it here is what let the
    header, Monitor and Work each count a different thing (#2207), so the set has
    one owner (:func:`kew.service.needs_you.needs_you_set`) and this function
    carries its entries, its ``unavailable`` names and its ``as_of`` stamps
    through unaltered.
    """
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    needs = needs_you_set(store, config, now=now)
    unavailable: list[str] = list(needs.unavailable)
    as_of: dict[str, str] = dict(needs.as_of)

    def source(name: str, fn: Callable[[], Any], default: Any) -> Any:
        """Run a reader; on any error, mark it unavailable and degrade."""
        try:
            value = fn()
        except Exception:  # noqa: BLE001 — fail-soft: one source never sinks the payload
            unavailable.append(name)
            return default
        as_of[name] = now_iso
        return value

    banner = source("banner", lambda: _banner(store, now), DashboardBannerDTO())

    glance = DashboardGlanceDTO(
        fleet=source("fleet", lambda: fleet_status(store), None),
        caps=source(
            "caps",
            lambda: governed_spend_windows(store, resolved_config_path(), now=now),
            None,
        ),
        routing=source("routing", lambda: _routing(config), []),
        ready_queue=source("ready_queue", lambda: _ready_issues_cached(config), []),
        queue_candidates=source(
            "queue_candidates", lambda: _queue_candidates_cached(config), []
        ),
        recent_completions=source(
            "recent_completions", lambda: _recent_completions(store), []
        ),
        # Read once, with the rows, from the same snapshot — never a second
        # time, which is how two halves of one console start disagreeing.
        pressure=needs.projection.pressure if needs.projection is not None else None,
    )

    return DashboardDTO(
        banner=banner,
        rows=needs.entries,
        glance=glance,
        as_of=as_of,
        unavailable=unavailable,
        needs_you_by_epic=needs_you_by_epic(needs.entries),
        needs_you_no_epic=needs_you_without_epic(needs.entries),
    )


# --------------------------------------------------------------------------- #
# Banner + glance sources
# --------------------------------------------------------------------------- #


def _banner(store: Any, now: datetime) -> DashboardBannerDTO:
    fleet_stopped = store.get_fleet_stopped() is not None
    # Archived epics are out (#2206) for the same reason a fresh repo's empty
    # roster is: a retired drain is not work waiting on a driver, so a roster
    # that is entirely archived has nothing to drive and raises no alarm.
    archived = archived_epic_refs(store)
    refs = [ref for ref in store.list_epic_refs() if ref not in archived]
    drive = read_drive_heartbeat(store)
    any_live = any(
        (lv := driver_liveness(store, ref, now=now, drive=drive)).present
        and not lv.driver_gone
        for ref in refs
    )
    # Only an alarm when epics exist but none is driving — a fresh repo with no
    # epics is not "no live driver", it just has nothing to drive.
    no_live_driver = bool(refs) and not any_live
    return DashboardBannerDTO(
        fleet_stopped=fleet_stopped, no_live_driver=no_live_driver
    )


def _route_str(entry: RouteEntry | str | None) -> str:
    if entry is None:
        return ""
    if isinstance(entry, str):
        return entry
    parts = [p for p in (entry.runner, entry.model) if p]
    return ":".join(parts)


def _routing(config: DispatchConfig) -> list[RouteRowDTO]:
    routing = config.routing
    if routing is None:
        return []
    default = _route_str(routing.default)
    rows = [RouteRowDTO(complexity="default", route=default, provenance="file")]
    for tier in ("simple", "medium", "complex"):
        entry = getattr(routing, tier)
        if entry is None:
            rows.append(
                RouteRowDTO(complexity=tier, route=default, provenance="default")
            )
        else:
            rows.append(
                RouteRowDTO(complexity=tier, route=_route_str(entry), provenance="file")
            )
    return rows


def _ready_issues_cached(config: DispatchConfig) -> list[ReadyIssueDTO]:
    """``_ready_issues`` behind a short per-repo TTL cache (see ``_ready_cache``).

    A raise from the reader propagates so ``source()`` marks the queue
    unavailable rather than serving a stale hit as if healthy.
    """
    repo = config.project.repo
    if not repo:
        return []
    return _cached_gh_read(
        f"ready:{config.labels.ready}",
        repo,
        _ready_cache,
        _READY_CACHE_TTL_S,
        lambda: _ready_issues(config),
    )


def ready_queue_item_ids(config: DispatchConfig | None) -> frozenset[str]:
    """The item ids the ready queue held at its last successful read.

    A peek, never a read: no gh call, and an expired entry still counts, because
    a row the console drew from that read is an item somebody can select. An
    empty set means nothing has been read for this repo yet.
    """
    repo = config.project.repo if config else ""
    hit = _ready_cache.get(repo) if repo else None
    if hit is None:
        return frozenset()
    return frozenset(issue.item_id or str(issue.issue_num) for issue in hit[1])


def ready_queue_tier(config: DispatchConfig | None, item: str | None) -> str | None:
    """The complexity label the ready queue read on ``item``, or ``None``.

    The same peek as ``ready_queue_item_ids``: no gh call, and an expired entry
    still counts. ``""`` is an item the queue holds with no tier label; ``None``
    is an item it does not hold, or a queue nothing has read yet.
    """
    repo = config.project.repo if config else ""
    hit = _ready_cache.get(repo) if repo and item else None
    for issue in hit[1] if hit else []:
        if (issue.item_id or str(issue.issue_num)) == item:
            return issue.complexity
    return None


def _ready_issues(config: DispatchConfig) -> list[ReadyIssueDTO]:
    """``[labels] ready``-labeled open issues via the non-lagging issues API.

    Never GH search (which lags). Repo is pinned server-side. A gh error or a
    malformed payload *raises* so the ``source`` wrapper marks the queue
    unavailable — an empty list here means "no ready issues", never "read
    failed". Unset repo returns empty (nothing to read, not a failure).
    """
    repo = config.project.repo
    if not repo:
        return []
    routing = config.routing
    result = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            # The CONFIGURED ready label (#2297). Hardcoding "ready" showed an
            # empty queue to every project that renamed it, while the dispatcher
            # read the right one — the page and the driver disagreed in silence.
            config.labels.ready,
            "--json",
            "number,title,labels",
            "--limit",
            "1000",
        ],
        check=False,
        timeout=_GH_TIMEOUT_S,
    )
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(
            f"gh issue list failed (rc={getattr(result, 'returncode', None)}): "
            f"stderr={getattr(result, 'stderr', '')!r}"
        )
    raw = json.loads(result.stdout)
    if not isinstance(raw, list):
        raise ValueError("gh issue list returned a non-list payload")
    out: list[ReadyIssueDTO] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if number is None:
            continue
        complexity = _complexity_of(item.get("labels"))
        out.append(
            ReadyIssueDTO(
                issue_num=int(number),
                item_id=str(int(number)),
                title=str(item.get("title", "")),
                complexity=complexity,
                route=_route_for(routing, complexity),
            )
        )
    return out


def _complexity_of(labels: Any) -> str:
    if not isinstance(labels, list):
        return ""
    names = {
        lbl.get("name") for lbl in labels if isinstance(lbl, dict) and lbl.get("name")
    }
    # The dispatcher's precedence (``selector.get_complexity``): complex, then
    # simple, then medium. An item labelled both simple and medium dispatches
    # as simple, so the queue and the launchpad must read it that way too.
    for tier in ("complex", "simple", "medium"):
        if tier in names:
            return tier
    return ""


def _route_for(routing: Any, complexity: str) -> str:
    if routing is None:
        return ""
    entry = getattr(routing, complexity, None) if complexity else None
    if entry is None:
        entry = routing.default
    return _route_str(entry)


def _queue_candidates_cached(config: DispatchConfig) -> list[QueueCandidateDTO]:
    """``_queue_candidates`` behind a per-repo TTL cache (see ``_candidates_cache``).

    An unset repo is never cached: the reader answers it without a gh call.
    """
    repo = config.project.repo
    if not repo:
        return _queue_candidates(config)
    return _cached_gh_read(
        "queue_candidates",
        repo,
        _candidates_cache,
        _CANDIDATES_CACHE_TTL_S,
        lambda: _queue_candidates(config),
    )


def _queue_candidates(config: DispatchConfig) -> list[QueueCandidateDTO]:
    """Open, un-ready issues of the pinned repo (number + title).

    Same non-lagging source as ``surfaced_candidates`` — ``gh issue list``
    against the same server-pinned repo, never GH search (which lags) — so the
    candidates offered here agree with what ``queue_issue`` will accept. A
    ready-labeled issue is already queued, so it is dropped (queuing it again is
    a no-op). A gh error or malformed payload *raises* so the ``source`` wrapper
    marks it unavailable — an empty list means "no un-ready issues", never "read
    failed". Unset repo returns empty (nothing to read, not a failure).
    """
    repo = config.project.repo
    if not repo:
        return []
    result = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,labels",
            "--limit",
            "1000",
        ],
        check=False,
        timeout=_GH_TIMEOUT_S,
    )
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(
            f"gh issue list failed (rc={getattr(result, 'returncode', None)}): "
            f"stderr={getattr(result, 'stderr', '')!r}"
        )
    raw = json.loads(result.stdout)
    if not isinstance(raw, list):
        raise ValueError("gh issue list returned a non-list payload")
    out: list[QueueCandidateDTO] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if number is None or _has_ready_label(item.get("labels"), config.labels.ready):
            continue
        out.append(
            QueueCandidateDTO(
                issue_num=int(number),
                item_id=str(int(number)),
                title=str(item.get("title", "")),
            )
        )
    return out


def _has_ready_label(labels: Any, ready: str) -> bool:
    """Whether ``labels`` (gh's list-of-dicts) carries the configured ready
    label — ``[labels] ready``, not the literal ``ready`` (#2297)."""
    if not isinstance(labels, list):
        return False
    return any(isinstance(lbl, dict) and lbl.get("name") == ready for lbl in labels)


def _finished_runs(store: Any) -> list[Any]:
    """The newest finished runs — SQL-side when the store can page (C3).

    Keeping five completions used to read every run in the ledger on every
    dashboard poll. A store with ``list_recent_completions`` answers with one
    bounded query; the narrow read-only fakes older callers inject keep the
    in-Python sort so their answer is identical, only costlier.
    """
    page = getattr(store, "list_recent_completions", None)
    if callable(page):
        rows: list[Any] = page(limit=_RECENT_COMPLETIONS)
        return rows
    finished = [r for r in store.list_all() if getattr(r, "finished_at", None)]
    finished.sort(key=lambda r: _iso(r.finished_at), reverse=True)
    return finished[:_RECENT_COMPLETIONS]


def _recent_completions(store: Any) -> list[CompletionDTO]:
    out = []
    for rec in _finished_runs(store):
        out.append(
            CompletionDTO(
                run_id=rec.run_id,
                issue_num=rec.issue_num,
                item_id=rec_item_id(rec),
                title=getattr(rec, "title", ""),
                status=rec.status,
                cost_usd=float(getattr(rec, "estimated_cost_usd", 0.0)),
                model=getattr(rec, "model", ""),
                finished_at=_iso(rec.finished_at),
            )
        )
    return out


__all__ = ["assemble"]
