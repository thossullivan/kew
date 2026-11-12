"""Audit-log read web/API surface (Task 8, F4 backend).

Store is injected (a RunStore-shaped object) and only the audit read methods
(`list_audit_events`, `audit_event_histogram`, `read_chain_rows`, plus
`latest_epic_tick` to resolve an epic's nodes) are touched — never imports
run_store, so the import-linter "serve reads through service" contract holds. Chain
verification reuses the exact primitives `kew audit verify` uses
(`kew.audit_chain`), so the web status can never drift from the CLI.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew.audit import DEFAULT_ANCHORS_PATH
from kew.audit_chain import ChainResult, classify_anchors, parse_anchors, verify_chain
from kew.ids import SQLITE_INT_MAX
from kew.service.models import (
    AuditEventDTO,
    AuditHistogramBucketDTO,
    AuditHistogramDTO,
    AuditListDTO,
    AuditStatusDTO,
    RunAuditEventDTO,
    RunAuditPageDTO,
)

MAX_LIMIT = 200
_SUMMARY_MAX = 200

#: Events one run-audit page may carry. The run page's event list polls this
#: while a run is live, so the page is capped the same way the feed is.
MAX_RUN_AUDIT = 200

#: Buckets one histogram request may ask for. The ``$.epic_ref`` arm of the
#: epic filter is unindexed, and a dense response is charted directly, so the
#: window/bucket pair is capped rather than silently coarsened.
MAX_BUCKETS = 500

# Status-verify cache. verify_chain is O(N) hashes; the audit dashboard polls
# /api/audit/status every couple of seconds, so we memoize the verdict.
#
# Key = (store_db_path, row_count, entry_hash_of_tail); value = (verified_at,
# result). The key changes on every append, so chain *growth* invalidates
# immediately. But an in-place edit of a NON-tail row changes neither the count
# nor the tail hash, so a warm entry would mask it — the TTL bounds that window:
# past _STATUS_CACHE_TTL_S the verdict is recomputed from a full chain read,
# which catches middle-row tamper. Status is therefore "verified as of at most
# _STATUS_CACHE_TTL_S ago", not instantaneously. The store db-path partitions
# the cache so one process serving multiple DBs can't cross-hit.
_STATUS_CACHE_TTL_S = 30.0
_status_cache: dict[tuple[str, int, str], tuple[float, ChainResult]] = {}


def _now() -> float:
    """Monotonic clock for cache TTL (patchable in tests)."""
    return time.monotonic()


def _store_key(store: Any) -> str:
    """Cache-partition identity: the store's DB path (falls back to object id)."""
    return str(getattr(store, "_db_path", None) or id(store))


def _summary(payload: Any) -> str | None:
    """Compact, size-bounded excerpt of the event payload (or None)."""
    if not isinstance(payload, dict) or not payload:
        return None
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
    return text if len(text) <= _SUMMARY_MAX else text[: _SUMMARY_MAX - 1] + "…"


def _epic_ref(payload: Any) -> str | None:
    """The event's epic attribution, read from the FULL payload.

    Deliberately not derived from ``_summary()``: that excerpt is capped at
    _SUMMARY_MAX characters, so a long payload would drop the attribution the
    client groups and filters by.
    """
    if not isinstance(payload, dict):
        return None
    ref = payload.get("epic_ref")
    return ref if isinstance(ref, str) and ref else None


def _to_dto(row: Any) -> AuditEventDTO:
    payload = getattr(row, "payload", None)
    return AuditEventDTO(
        id=row.id,
        timestamp=row.timestamp,
        event=row.event,
        issue_num=getattr(row, "issue_num", None),
        item_id=getattr(row, "item_id", None),
        run_id=getattr(row, "run_id", None),
        actor_type=getattr(row, "actor_type", None),
        actor_id=getattr(row, "actor_id", None),
        summary=_summary(payload),
        epic_ref=_epic_ref(payload),
    )


def _epic_node_ids(store: Any, epic: str) -> list[str]:
    """Node ids of the epic's latest journaled snapshot (empty when unknown).

    The snapshot is read as plain JSON, never through kew.graph — the same
    layering boundary kew.service.epics holds. An epic with no journal yet
    still resolves: the ``$.epic_ref`` payload arm matches its epic-level
    events on its own.
    """
    tick = store.latest_epic_tick(epic)
    snapshot = getattr(tick, "snapshot", None) if tick is not None else None
    if not isinstance(snapshot, dict):
        return []
    nodes = snapshot.get("nodes") or []
    if not isinstance(nodes, list):
        return []
    ids = [str(n.get("id", "")) for n in nodes if isinstance(n, dict)]
    return [i for i in ids if i]


def _scope_filters(
    store: Any,
    *,
    event: str | None,
    actor_type: str | None,
    issue: int | None,
    since: datetime | None,
    until: datetime | None,
    item: str | None,
    epic: str | None,
    event_prefixes: Sequence[str] | None,
) -> dict[str, Any]:
    """Store kwargs shared by the page read and the histogram, so a filter can
    never mean one thing on the feed and another on the chart."""
    return {
        "event": event,
        "actor_type": actor_type,
        "issue_num": issue,
        "since": _as_utc(since),
        "until": _as_utc(until),
        "item_or_issue": item or None,
        "epic_ref": epic or None,
        "epic_node_ids": _epic_node_ids(store, epic) if epic else None,
        "event_prefixes": list(event_prefixes or ()),
    }


def _utc(value: datetime) -> datetime:
    """Timestamps reach the store tz-aware; a naive query param means UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _as_utc(value: datetime | None) -> datetime | None:
    """:func:`_utc` for a filter that may simply be absent."""
    return None if value is None else _utc(value)


def list_audit(
    store: Any,
    *,
    event: str | None = None,
    actor_type: str | None = None,
    issue: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    item: str | None = None,
    epic: str | None = None,
    event_prefixes: Sequence[str] | None = None,
    limit: int = 100,
    after: int | None = None,
) -> AuditListDTO:
    """Newest-first page of audit events with filters + an `after` id cursor.

    `after` is a strict upper-exclusive id bound (events with ``id < after``),
    which pairs with the newest-first ordering to page backwards through the
    log. `next_after` is set only when more filtered events exist beyond the
    returned page. ``since``/``until`` bound the window (``until`` exclusive)
    and are applied in the store, so paging inside a window returns every
    event in it exactly once.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    # Fetch limit+1 to detect a further page; all filters are pushed into the
    # store query — no post-filtering in Python.
    rows = store.list_audit_events(
        limit + 1,
        after_id=after,
        **_scope_filters(
            store,
            event=event,
            actor_type=actor_type,
            issue=issue,
            since=since,
            until=until,
            item=item,
            epic=epic,
            event_prefixes=event_prefixes,
        ),
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_after = page[-1].id if has_more and page else None
    return AuditListDTO(events=[_to_dto(r) for r in page], next_after=next_after)


def _to_run_event_dto(row: Any) -> RunAuditEventDTO:
    payload = getattr(row, "payload", None)
    return RunAuditEventDTO(
        seq=row.id,
        ts=row.timestamp,
        event=row.event,
        actor_type=getattr(row, "actor_type", None),
        item_id=getattr(row, "item_id", None),
        payload=payload if isinstance(payload, dict) else None,
    )


def run_audit_page(
    store: Any,
    run_id: str,
    *,
    after_seq: int = 0,
    limit: int = MAX_RUN_AUDIT,
) -> RunAuditPageDTO:
    """One forward page of a run's ledger events, oldest first (C3/#2300).

    The feed `/api/audit` serves pages BACKWARDS — newest first, with `after=`
    an upper-exclusive bound — which is the wrong direction for a run page: it
    opens at the run's first event and walks forward, and a live run resumes
    from the cursor it last read. So the window here is the store's forward
    cursor (``after_seq``, strict lower-exclusive, ascending), narrowed to this
    run by the ``audit_events.run_id`` index. Both are SQL predicates, so the
    cost of a page is the page.

    An out-of-range ``limit`` is refused rather than clamped: a caller that
    asked for 10,000 events is asking a different question from the one a
    silently shortened page answers (the same rule :func:`list_runs` holds).
    """
    if not 1 <= limit <= MAX_RUN_AUDIT:
        raise ValueError(f"limit must be between 1 and {MAX_RUN_AUDIT}")
    rows = store.list_audit_events(limit, run_id=run_id, after_seq=after_seq)
    events = [_to_run_event_dto(row) for row in rows]
    return RunAuditPageDTO(
        run_id=run_id,
        events=events,
        # Where to resume, not a has-more flag: an empty page holds the cursor
        # it was asked with, so a live reader polls on without a special case.
        next_seq=events[-1].seq if events else after_seq,
    )


def bucket_count(since: datetime, until: datetime, bucket_s: int) -> int:
    """How many buckets ``[since, until)`` splits into at this width.

    Raises ValueError when the window is empty or backwards, when the width is
    outside SQLite's integer bind range, or when it would need more than
    :data:`MAX_BUCKETS` — the caller turns that into a 422.

    The width is checked against :data:`~kew.ids.SQLITE_INT_MAX` *before* it is
    divided into the span: a width past int64 needs exactly one bucket, so the
    cap below waves it through, and it then raises ``OverflowError`` out of the
    sqlite3 bind (or out of the division itself, once the value is too wide to
    convert to a float) — a 500 for a window that is simply not answerable.
    """
    if bucket_s < 1:
        raise ValueError("bucket_s must be at least 1 second")
    if bucket_s > SQLITE_INT_MAX:
        raise ValueError(f"bucket_s must be at most {SQLITE_INT_MAX} seconds")
    span = (_utc(until) - _utc(since)).total_seconds()
    if span <= 0:
        raise ValueError("until must be after since")
    count = math.ceil(span / bucket_s)
    if count > MAX_BUCKETS:
        raise ValueError(
            f"{count} buckets requested; at most {MAX_BUCKETS} per request"
        )
    return count


def audit_histogram(
    store: Any,
    *,
    since: datetime,
    until: datetime,
    bucket_s: int,
    event: str | None = None,
    actor_type: str | None = None,
    issue: int | None = None,
    item: str | None = None,
    epic: str | None = None,
    event_prefixes: Sequence[str] | None = None,
) -> AuditHistogramDTO:
    """Dense per-event counts over ``[since, until)``, aggregated in the store.

    Every bucket in the window is present (empty ones with no counts) so the
    client charts the series directly. The bucket counts sum to ``total``,
    which is by construction the number of events a paged read over the same
    filters would walk.
    """
    count = bucket_count(since, until, bucket_s)
    filters = _scope_filters(
        store,
        event=event,
        actor_type=actor_type,
        issue=issue,
        since=since,
        until=until,
        item=item,
        epic=epic,
        event_prefixes=event_prefixes,
    )
    rows = store.audit_event_histogram(bucket_s=bucket_s, **filters)
    buckets: list[dict[str, int]] = [{} for _ in range(count)]
    total = 0
    for index, name, n in rows:
        # Clamped, never dropped. The store applied the same [since, until), so
        # an index outside the range can only be the millisecond rounding at the
        # `until` edge. Dropping that row would make `total` disagree with a
        # paged read; counting it with no bucket would make the buckets
        # disagree with `total`. Both hold if it lands in the edge bucket.
        slot = buckets[min(max(index, 0), count - 1)]
        key = str(name)
        slot[key] = slot.get(key, 0) + n
        total += n
    start = _utc(since)
    return AuditHistogramDTO(
        since=start.isoformat(),
        until=_utc(until).isoformat(),
        bucket_s=bucket_s,
        buckets=[
            AuditHistogramBucketDTO(
                start=(start + timedelta(seconds=bucket_s * i)).isoformat(),
                counts=counts,
            )
            for i, counts in enumerate(buckets)
        ],
        total=total,
    )


def load_anchors(repo_dir: Path | None) -> list[Any] | None:
    """Parsed anchors from the default file under repo_dir, or None if absent.

    Public entry point shared by ``audit_status`` and the evidence-pack
    attestation (``report_export``) so both resolve anchors identically.
    """
    if repo_dir is None:
        return None
    path = Path(repo_dir) / DEFAULT_ANCHORS_PATH
    if not path.exists():
        return None
    try:
        return parse_anchors(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_lookup(store: Any) -> tuple[tuple[str, int, str] | None, ChainResult | None]:
    """Return (cache_key, cached_result) — result is None on miss or TTL expiry."""
    read_head = getattr(store, "read_chain_head_key", None)
    head = read_head() if callable(read_head) else None
    if head is None:
        return None, None
    cache_key = (_store_key(store), *head)
    entry = _status_cache.get(cache_key)
    if entry is not None and _now() - entry[0] < _STATUS_CACHE_TTL_S:
        return cache_key, entry[1]
    return cache_key, None


def _cache_store(cache_key: tuple[str, int, str] | None, result: ChainResult) -> None:
    """Memoize a fresh verdict. Stale keys are never re-used, so keep size O(1)."""
    if cache_key is not None:
        _status_cache.clear()
        _status_cache[cache_key] = (_now(), result)


def chain_verdict(store: Any) -> ChainResult | None:
    """The memoized full-chain verdict, or None when this store has no chain.

    One verdict, shared by every reader that needs it: ``/api/audit/status``
    polls it every couple of seconds and EVERY Flight replay reports it
    (``chain_verified``), and a Flight hover preload fires on mouse-over.
    ``verify_chain`` hashes the whole ledger, so re-verifying per call made the
    cost of looking at a run grow with the audit history of the whole project.

    The cache is the one documented at the top of this module: keyed on
    (store, row_count, tail hash) and TTL-bounded, so a non-tail in-place edit
    can be masked for at most ``_STATUS_CACHE_TTL_S``. Sharing the entry does
    not widen that window — the window is a property of the key and the TTL,
    not of how many readers hit it.
    """
    cache_key, cached = _cache_lookup(store)
    if cached is not None:
        return cached
    read_rows = getattr(store, "read_chain_rows", None)
    if not callable(read_rows):
        return None
    result = verify_chain(read_rows())
    _cache_store(cache_key, result)
    return result


def audit_status(store: Any, *, repo_dir: Path | None = None) -> AuditStatusDTO:
    """Verify the hash chain (and anchors, if present) — the CLI's verdict.

    verify_chain is O(N); the dashboard hot-polls this endpoint, so the verdict
    is memoized keyed on (store, row_count, entry_hash_of_tail) with a
    _STATUS_CACHE_TTL_S TTL. The key changes on every append, so chain *growth*
    invalidates immediately. An in-place edit of a non-tail row changes neither
    the count nor the tail hash; the TTL bounds how long such a modification can
    be masked (re-verified from a full read at most every _STATUS_CACHE_TTL_S).
    Anchor classification always runs fresh (the anchor file is external).
    """
    cache_key, cached = _cache_lookup(store)
    anchors = load_anchors(repo_dir)

    if cached is not None and anchors is None:
        return AuditStatusDTO(
            events=cached.count,
            verified=cached.ok,
            anchors_matched=0,
            anchors_total=0,
        )

    rows = store.read_chain_rows()
    if cached is None:
        result = verify_chain(rows)
        _cache_store(cache_key, result)
    else:
        result = cached

    if anchors is None:
        return AuditStatusDTO(
            events=result.count,
            verified=result.ok,
            anchors_matched=0,
            anchors_total=0,
        )
    report = classify_anchors(rows, anchors)
    return AuditStatusDTO(
        events=result.count,
        verified=result.ok and report.ok,
        anchors_matched=report.matched,
        anchors_total=len(anchors),
    )
