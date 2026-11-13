"""Item description read (FUI-31) — the tracker's issue body, cached.

The DWork description panel (artboard x 468–853, y ≈624–928) shows the words a
person wrote on the tracker, which kew records nowhere: every other projection
on the Work screen is composed from the local store, and this one is a *remote*
read. It exists under the owner's ruling of 2026-09-24 on #2056, which moves
software-factory information into the web UI in kew; FUI-21 renders it and
keeps its "Open on GitHub" link as the fallback for every answer below that is
not ``available``.

Five rules shape the module.

**A failed read is never an empty description.** ``availability`` is the same
vocabulary :class:`~kew.service.models.RemoteObservationDTO` uses — an empty
``description`` is reportable only under ``available``, i.e. a read that
succeeded and genuinely found nothing. Anything else carries ``reason``.

**The read is cached, and the cache is bounded.** A tracker read costs a
subprocess (GitHub) or an API call (Linear), and the Work screen polls. Entries
live :data:`DESCRIPTION_TTL_S` seconds, keyed by tracker, source and item so one
process serving two repositories cannot cross-hit. A refresh that fails does not
discard the last reading — it serves it as ``stale`` with the failure's reason
attached, because an old description is worth more to a reader than none.

**Every read is bounded, in time, in parallelism and in rate.** A browser is
waiting, so the read gets :data:`READ_TIMEOUT_S` and one attempt — not the
retry schedule a background tick can afford — and at most
:data:`MAX_CONCURRENT_READS` reads are in flight process-wide. The serve route
is a synchronous one, so an in-flight read holds a worker thread, and the cache
cannot bound that: the item id comes from the client, and a novel id is always
a cold miss. For the same reason the reads are rationed over time as well —
:data:`MAX_READS_PER_WINDOW` per :data:`READ_BUDGET_WINDOW_S` — because the
GitHub rate budget this spends is the dispatch driver's too, and a caller
walking novel ids would otherwise recycle those four slots as fast as the
tracker answers. Past either bound a read is refused at once, which under the
first rule is an answer the panel can draw. What the ration counts is what left
the box: a read refused for a reason that costs the tracker nothing — an id that
is not an issue number, an unconfigured repo, a tracker with no read, a missing
Linear key, or either bound itself — spends none of it, or the ration would be a
way to blank every uncached read from behind the guard at no off-box cost.

**One read per item, not one per poll.** Inside the process-wide bound sits a
per-key gate: while a read of an item is out at the tracker, a second read of
that *same* item is refused without being attempted. The Work screen polls, so
duplicates are the common case, and without the gate they are the expensive
one — they multiply tracker load by the poll fan-out and spend the whole
process-wide bound on ONE item, refusing every other item's cold or expired
read. A refused duplicate costs the caller nothing it did not already have: the
read that won is filling the very cache entry the duplicate would have filled.

**The split is structural, not clever.** ``expected`` and ``actual`` are filled
only from headings that say so; everything else stays in ``description``, in
order. Nothing is inferred from position, and a ``#`` inside a fenced code block
is code, not a heading.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from kew.linear.client import LinearApiError, LinearClient
from kew.linear.tracker import LinearTracker
from kew.service.models import ItemDescriptionDTO
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

#: How long a reading may be served without touching the tracker again. An
#: issue body changes when a person edits it, which is rare next to the Work
#: screen's poll rate; two minutes bounds how long an edit stays invisible.
DESCRIPTION_TTL_S = 120.0

#: Most readings retained. The Work screen reads one item at a time, so this is
#: a leak bound rather than a working-set size: past it, the coldest entries go.
CACHE_MAX_ENTRIES = 64

#: Payload bound. A tracker body is user-supplied text of no fixed size, and
#: this read answers a browser panel; a longer body is clipped, never refused.
MAX_BODY_CHARS = 20_000

#: How long the tracker read may take before it is reported unavailable. It
#: bounds the ``gh`` subprocess outright, and the socket operations of the ONE
#: Linear attempt this read makes (see :func:`_linear_client`).
READ_TIMEOUT_S = 20.0

#: How many tracker reads may be in flight at once, process-wide. The serve
#: route is a ``def``, not an ``async def``, so every read in flight holds one
#: of anyio's worker threads (40 by default) for as long as the tracker takes:
#: unbounded, a burst of reads for ids nothing has cached would leave the rest
#: of the app's synchronous routes waiting behind a remote tracker. Past this
#: many, a read is refused immediately rather than queued — a refusal costs the
#: caller a reason (or the last reading, as ``stale``), and the panel has a
#: fallback for both. It bounds reads of DISTINCT items: a duplicate read of one
#: item is turned away ahead of it, by :func:`_admitted`, and spends no slot.
MAX_CONCURRENT_READS = 4

#: How many tracker reads may be STARTED per :data:`READ_BUDGET_WINDOW_S`,
#: process-wide. :data:`MAX_CONCURRENT_READS` bounds what this read holds at one
#: instant; this bounds what it SPENDS over time, and the two are not the same
#: thing — four slots recycled as fast as a tracker answers is an unbounded
#: request rate against the operator's GitHub credentials, whose rate budget the
#: dispatch driver shares. The cache cannot bound it either: the item id comes
#: from the client, so a caller walking novel ids misses every time and pays a
#: tracker read every time. This is that ceiling, far above what the Work screen
#: costs (one read per item per TTL, so ~one a minute for an operator moving
#: briskly between items) and far below a spend worth noticing.
MAX_READS_PER_WINDOW = 30
READ_BUDGET_WINDOW_S = 60.0

NO_REPO_REASON = "no delivery repository is configured (project.repo)"
NOT_A_GITHUB_NUMBER = "{item_id} is not a GitHub issue number"
NO_LINEAR_KEY_REASON = (
    "tracker.type is 'linear' but the LINEAR_API_KEY environment variable is not set"
)
UNSUPPORTED_TRACKER = "tracker '{tracker}' has no description read"
UNREADABLE_JSON = "the tracker returned a response this read could not parse"
GH_FAILED = "gh could not read the issue"
BUSY_REASON = "{limit} tracker reads are already in flight; this one was not attempted"
SAME_ITEM_REASON = (
    "a read for this item is already in flight; this one was not attempted"
)
BUDGET_REASON = (
    "the tracker read budget ({limit} per {window:.0f}s) is spent; "
    "this one was not attempted"
)


class TrackerReadError(Exception):
    """A tracker read that did not produce a body, and why."""


@dataclass(frozen=True)
class Reading:
    """One successful read of an item's body, and when it happened."""

    title: str
    url: str | None
    body: str
    observed_at: datetime


class _Request(Protocol):
    """The ONE tracker request a read will make, resolved but not yet made."""

    def __call__(self, *, now: datetime) -> Reading: ...


_cache: dict[tuple[str, str, str], tuple[float, Reading]] = {}
_cache_lock = threading.Lock()
_in_flight = threading.BoundedSemaphore(MAX_CONCURRENT_READS)

#: The cache keys whose read is out at the tracker right now — a single-flight
#: register, and deliberately not a lock: a duplicate poll is turned away, never
#: parked behind the read it duplicates. Guarded by its own lock, which is never
#: held across a tracker read.
_reading_keys: set[tuple[str, str, str]] = set()
_keys_lock = threading.Lock()

#: When each tracker read of the current window started, oldest first — the
#: read-budget ledger. Only starts are recorded: what it rations is requests
#: made of the tracker, not time spent waiting for them. And only reads that
#: reach the tracker are recorded at all: a read refused at a gate, or refused
#: before there is a request to make (:func:`_request`), never appears here.
_read_starts: deque[float] = deque()
_budget_lock = threading.Lock()


def _now() -> float:
    """Monotonic clock for the cache TTL (patchable in tests)."""
    return time.monotonic()


def clear_cache() -> None:
    """Drop every cached reading (used by tests and by a config change)."""
    with _cache_lock:
        _cache.clear()


def reset_read_budget() -> None:
    """Forget the current window's reads (tests; never a request path).

    Separate from :func:`clear_cache` on purpose: dropping the readings makes
    the next read a miss, and a miss must still be paid for out of the budget,
    or clearing the cache would be a way to refill it.
    """
    with _budget_lock:
        _read_starts.clear()


# --------------------------------------------------------------------------- #
# Reading the tracker.
# --------------------------------------------------------------------------- #


def _github_reading(item_id: str, repo: str, *, now: datetime) -> Reading:
    """One issue body from ``gh``. The repo is pinned server-side, never sent.

    Its arguments are already vetted by :func:`_github_request`, which runs
    before any gate: reaching here is reaching the tracker.
    """
    try:
        result = run(
            [
                "gh",
                "issue",
                "view",
                item_id,
                "--repo",
                repo,
                "--json",
                "title,body,url",
            ],
            check=False,
            timeout=READ_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise TrackerReadError(f"{GH_FAILED}: {exc}") from exc
    if getattr(result, "returncode", 1) != 0:
        detail = str(getattr(result, "stderr", "") or "").strip()[:200]
        raise TrackerReadError(f"{GH_FAILED}: {detail or 'non-zero exit'}")
    return _reading(_json_object(result.stdout), now=now)


def _linear_client(api_key: str) -> LinearClient:
    """A Linear client on this read's budget, not the epic driver's.

    :class:`~kew.linear.client.LinearClient`'s defaults are a background tick's:
    three attempts, a 30s socket and 1s+2s of backoff between them, ~93s before
    it gives up. Nothing waits on that tick; a browser panel waits on this read.
    So it gets ONE attempt, bounded by :data:`READ_TIMEOUT_S` like the ``gh``
    path beside it — a tracker that is slow or down is an answer this module
    already knows how to give (``unavailable``, or the last reading as
    ``stale``), and it is worth more to a reader than a held request.
    """
    return LinearClient(api_key, max_retries=1, timeout=READ_TIMEOUT_S)


def _linear_reading(
    item_id: str, config: DispatchConfig, api_key: str, *, now: datetime
) -> Reading:
    """One issue body from Linear, whose ``fetch_issue`` already normalizes it.

    The key is found by :func:`_linear_request` ahead of the gates, so a read
    that gets here is one that will be made.
    """
    tracker = LinearTracker(config, _linear_client(api_key))
    try:
        issue = tracker.fetch_issue(item_id)
    except (LinearApiError, OSError, ValueError) as exc:
        raise TrackerReadError(f"the Linear read failed: {exc}") from exc
    return _reading(dict(issue), now=now)


def _json_object(payload: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise TrackerReadError(f"{UNREADABLE_JSON}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TrackerReadError(UNREADABLE_JSON)
    return parsed


def _reading(issue: dict[str, Any], *, now: datetime) -> Reading:
    url = issue.get("url")
    return Reading(
        title=str(issue.get("title") or ""),
        url=str(url) if url else None,
        body=_clip(str(issue.get("body") or "")),
        observed_at=now,
    )


def _clip(text: str) -> str:
    return text if len(text) <= MAX_BODY_CHARS else text[: MAX_BODY_CHARS - 1] + "…"


def _source(config: DispatchConfig) -> str:
    """The cache-partitioning identity of what is being read from."""
    if config.tracker.type == "linear":
        return f"linear:{config.tracker.linear.team}"
    return f"github:{config.project.repo or ''}"


def _claim(key: tuple[str, str, str]) -> bool:
    """Register ``key`` as being read. False when a read already holds it."""
    with _keys_lock:
        if key in _reading_keys:
            return False
        _reading_keys.add(key)
        return True


def _unclaim(key: tuple[str, str, str]) -> None:
    with _keys_lock:
        _reading_keys.discard(key)


def _spend_budget() -> None:
    """Take one of this window's tracker reads, or refuse the read."""
    at = _now()
    with _budget_lock:
        while _read_starts and at - _read_starts[0] >= READ_BUDGET_WINDOW_S:
            _read_starts.popleft()
        if len(_read_starts) >= MAX_READS_PER_WINDOW:
            raise TrackerReadError(
                BUDGET_REASON.format(
                    limit=MAX_READS_PER_WINDOW, window=READ_BUDGET_WINDOW_S
                )
            )
        _read_starts.append(at)


@contextmanager
def _slot() -> Iterator[None]:
    """Hold one of the :data:`MAX_CONCURRENT_READS` in-flight slots."""
    if not _in_flight.acquire(blocking=False):
        raise TrackerReadError(BUSY_REASON.format(limit=MAX_CONCURRENT_READS))
    try:
        yield
    finally:
        _in_flight.release()


@contextmanager
def _admitted(key: tuple[str, str, str]) -> Iterator[None]:
    """Admit ONE read of ``key``, inside both bounds this module keeps.

    Every gate refuses rather than queues: the read runs on the worker thread
    the serve route was handed, and holding that thread is the one thing the
    in-flight bound exists to protect. The order is what makes each bound mean
    what it says. The per-key gate is taken FIRST and given back LAST, so a
    duplicate read of one item neither spends the budget nor takes a slot, and
    cannot starve another item's read. The budget is taken LAST, with the slot
    already held and nothing left between it and the tracker, because it rations
    requests the tracker actually receives: a read refused for being busy is one
    the tracker never sees, and charging it would let a caller spend the ration
    without ever leaving the box. Holding a slot across that check costs nothing
    the in-flight bound protects — what it protects the worker threads from is a
    REMOTE read, and the check is a scan of a deque of at most
    :data:`MAX_READS_PER_WINDOW` timestamps. Every path out — a refusal at any
    gate, a failed read, a successful one — gives back exactly what it took.
    """
    if not _claim(key):
        raise TrackerReadError(SAME_ITEM_REASON)
    try:
        with _slot():
            _spend_budget()
            yield
    finally:
        _unclaim(key)


def _read(
    item_id: str, config: DispatchConfig, *, key: tuple[str, str, str], now: datetime
) -> Reading:
    """One tracker read: resolved by :func:`_request`, admitted by
    :func:`_admitted`. Resolution comes first so that a read which will make no
    request is refused before it holds, spends or charges anything."""
    request = _request(item_id, config)
    with _admitted(key):
        return request(now=now)


def _request(item_id: str, config: DispatchConfig) -> _Request:
    """The ONE tracker request this read will make, or the refusal replacing it.

    Every refusal that issues no tracker request is made here, ahead of the
    gates in :func:`_admitted`: an unsupported tracker, an unset
    ``project.repo``, an id that is not a GitHub issue number, a Linear key that
    is not set. Such a read costs nothing off-box, so it must cost nothing
    on-box either — the ledger :func:`_spend_budget` keeps records requests made
    of the tracker, and a caller who can charge it without making one can blank
    every uncached read for a window from behind the guard, for free.
    """
    tracker = config.tracker.type
    if tracker == "github":
        return _github_request(item_id, config)
    if tracker == "linear":
        return _linear_request(item_id, config)
    raise TrackerReadError(UNSUPPORTED_TRACKER.format(tracker=tracker))


def _github_request(item_id: str, config: DispatchConfig) -> _Request:
    """The ``gh`` read of an issue number, once there is one to read."""
    repo = config.project.repo
    if not repo:
        raise TrackerReadError(NO_REPO_REASON)
    # ASCII digits only: ``str.isdigit`` is true for "٢" too, and a non-ASCII
    # "number" is not an issue reference — it is an argument nobody vetted.
    if not (item_id.isascii() and item_id.isdigit()):
        raise TrackerReadError(NOT_A_GITHUB_NUMBER.format(item_id=item_id))
    return partial(_github_reading, item_id, repo)


def _linear_request(item_id: str, config: DispatchConfig) -> _Request:
    """The Linear read of an item, once there is a key to make it with."""
    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        raise TrackerReadError(NO_LINEAR_KEY_REASON)
    return partial(_linear_reading, item_id, config, api_key)


# --------------------------------------------------------------------------- #
# Splitting the body.
# --------------------------------------------------------------------------- #

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SECTION = re.compile(r"^(expected|actual)\b")
_DECORATION = "#*_: \t"
#: The three parts, in the order :func:`split_body` returns them.
_PARTS = ("description", "expected", "actual")


def _section_of(heading: str) -> str | None:
    """``expected``/``actual`` when the heading says so, else ``None``."""
    match = _SECTION.match(heading.strip().strip(_DECORATION).lower())
    return match.group(1) if match else None


@dataclass
class _Split:
    """Line-by-line state of a body being split into its three parts.

    Small on purpose: a heading's meaning depends on where the reader already
    is (inside a fence, inside a section), and that context is the whole
    algorithm. ``depth`` is the level of the heading that opened the current
    section, so a deeper heading is a subsection of it and a shallower one ends
    it; ``seen`` keeps the FIRST "Expected" from being reopened by a second.
    """

    parts: dict[str, list[str]] = field(
        default_factory=lambda: {"description": [], "expected": [], "actual": []}
    )
    target: str = "description"
    depth: int = 0
    fence: str = ""
    seen: set[str] = field(default_factory=set)

    def feed(self, line: str) -> None:
        """Route one line to the part it belongs to."""
        if self.fence:
            if line.strip().startswith(self.fence):
                self.fence = ""
        elif opening := _FENCE.match(line):
            self.fence = opening.group(1)
        elif (heading := _HEADING.match(line)) and self._heading(
            len(heading.group(1)), heading.group(2)
        ):
            return  # the section heading itself: consumed, not content
        self.parts[self.target].append(line)

    def _heading(self, level: int, text: str) -> bool:
        """Open or close a section. True when the heading line is consumed."""
        section = _section_of(text)
        if section is not None and section not in self.seen:
            self.seen.add(section)
            self.target, self.depth = section, level
            return True
        if self.target != "description" and level <= self.depth:
            self.target, self.depth = "description", 0
        return False


def split_body(body: str) -> tuple[str, str, str]:
    """Split a tracker body into ``(description, expected, actual)``.

    A heading that names a section opens it and is itself consumed (the panel
    draws its own label); the section runs until a heading at the same level or
    higher, so its subsections stay inside it. Every other line — including
    every other heading, with its own text — stays in ``description``, in the
    order it was written. A body with no such headings is all description.
    """
    split = _Split()
    for line in body.splitlines():
        split.feed(line)
    description, expected, actual = (_tidy(split.parts[part]) for part in _PARTS)
    return description, expected, actual


def _tidy(lines: list[str]) -> str:
    return _clip("\n".join(lines).strip())


# --------------------------------------------------------------------------- #
# Composition.
# --------------------------------------------------------------------------- #


def _cached(key: tuple[str, str, str]) -> tuple[float, Reading] | None:
    with _cache_lock:
        return _cache.get(key)


def _store(key: tuple[str, str, str], reading: Reading, *, at: float) -> None:
    with _cache_lock:
        _cache[key] = (at, reading)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)


def _dto(
    item_id: str,
    reading: Reading | None,
    *,
    availability: str,
    provenance: str,
    reason: str,
    moment: datetime,
) -> ItemDescriptionDTO:
    description, expected, actual = (
        split_body(reading.body) if reading is not None else ("", "", "")
    )
    return ItemDescriptionDTO(
        item_id=item_id,
        availability=availability,
        provenance=provenance,
        observed_at=(
            None if reading is None else reading.observed_at.astimezone(UTC).isoformat()
        ),
        title="" if reading is None else reading.title,
        url=None if reading is None else reading.url,
        description=description,
        expected=expected,
        actual=actual,
        reason=reason,
        composed_at=moment.isoformat(),
    )


def item_description(
    item_id: str,
    *,
    config: DispatchConfig,
    now: datetime | None = None,
) -> ItemDescriptionDTO:
    """The tracker's words for ONE item, read through the TTL cache.

    Never raises for a tracker that is down, absent or misconfigured: the answer
    is ``unavailable`` with the reason, or — when a reading is already held —
    ``stale`` with the same reason and the last body still attached. The item id
    stays opaque: it is the tracker's own key, parsed only where the provider
    requires it (a GitHub issue number is a number).
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    key = (config.tracker.type, _source(config), item_id)
    entry = _cached(key)
    at = _now()
    if entry is not None and at - entry[0] < DESCRIPTION_TTL_S:
        return _dto(
            item_id,
            entry[1],
            availability="available",
            provenance="cached",
            reason="",
            moment=moment,
        )
    try:
        reading = _read(item_id, config, key=key, now=moment)
    except TrackerReadError as exc:
        reason = str(exc)
        _logger.warning("description read for %s is unavailable: %s", item_id, reason)
        if entry is None:
            return _dto(
                item_id,
                None,
                availability="unavailable",
                provenance="error",
                reason=reason,
                moment=moment,
            )
        return _dto(
            item_id,
            entry[1],
            availability="stale",
            provenance="cached",
            reason=reason,
            moment=moment,
        )
    _store(key, reading, at=at)
    return _dto(
        item_id,
        reading,
        availability="available",
        provenance="observed",
        reason="",
        moment=moment,
    )
