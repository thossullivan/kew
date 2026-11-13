"""Item description read (FUI-31) — the tracker body, cached, and its route.

The DWork description panel (x 468–853, y ≈624–928) is the only *remote* read
on the Work screen: every other projection is composed from the local store.
That asymmetry is what this file guards, from four directions.

* **A failure is never an empty description.** ``availability`` carries the
  answer — a body is reportable as empty only under ``available`` — and every
  other answer carries a reason for FUI-21's "Open on GitHub" fallback to show.
* **The cache bounds the reads, not the truth.** A second read inside the TTL
  touches no tracker; past it the tracker is read again; and a refresh that
  fails serves the last reading as ``stale`` with the failure attached rather
  than throwing a usable description away.
* **Every bound holds.** The Linear read makes ONE attempt inside
  ``READ_TIMEOUT_S`` rather than inheriting the client's background-tick retry
  schedule; no more than ``MAX_CONCURRENT_READS`` reads are in flight at once —
  the route is a synchronous one, so an unbounded burst of cold ids would hold
  the app's worker threads behind a remote tracker; no more than
  ``MAX_READS_PER_WINDOW`` are STARTED per window, because those four slots
  recycled at tracker speed spend a GitHub rate budget the dispatch driver
  shares; only ONE read per item is in flight, so the polls of a single id
  spend neither that bound nor that budget between them; and a read that fails
  gives back both the slot and the item it took, or the bounds leak shut one
  failure at a time.
* **The split is structural.** ``expected``/``actual`` come from headings that
  say so and nothing else — not from position, and not from a ``#`` that is
  really code.

The tracker seam is the only thing mocked: ``run`` (for ``gh``) and Linear's
tracker class. Guard coverage starts where ``test_serve_api_evidence`` does —
the router carries ``read_guard``, so the read is session-gated under
``--expose`` and for every non-loopback peer, loopback-open otherwise — and adds
the one thing this route does not share with its neighbours: ``offbox_read``
refuses a cross-site driver, because a page the operator merely visits must not
be able to spend a subprocess and a tracker request it can never read back.
``tests/test_serve_security.py`` owns that guard's own unit coverage.
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import kew.service.description as desc
from kew.config import DispatchConfig, ProjectConfig, TrackerConfig
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.service.description import (
    BUDGET_REASON,
    BUSY_REASON,
    DESCRIPTION_TTL_S,
    MAX_BODY_CHARS,
    MAX_CONCURRENT_READS,
    MAX_READS_PER_WINDOW,
    NO_LINEAR_KEY_REASON,
    NO_REPO_REASON,
    READ_BUDGET_WINDOW_S,
    SAME_ITEM_REASON,
    item_description,
    split_body,
)

ITEM = "2055"
PATH = f"/api/work/{ITEM}/description"
URL = "https://github.com/kew-dev/kew/issues/2055"
MOMENT = datetime(2026, 9, 24, 18, 12, 4, tzinfo=UTC)

BODY = """The Work screen shows no description at all.

## Expected

The issue body, split into its sections.

### Detail

Headings below the section heading stay inside it.

## Actual

An empty panel.

## Notes

Everything else stays in the description.
"""


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    """Cache and read budget are module state: isolate both so tests stay
    order-independent, and so one test's reads are not charged to the next."""
    desc.clear_cache()
    desc.reset_read_budget()
    yield
    desc.clear_cache()
    desc.reset_read_budget()


def _config(**overrides: Any) -> DispatchConfig:
    fields: dict[str, Any] = {"project": ProjectConfig(repo="kew-dev/kew")}
    fields.update(overrides)
    return DispatchConfig(**fields)


def _gh(
    body: str = BODY, *, title: str = "The Work screen has no description"
) -> subprocess.CompletedProcess[str]:
    import json

    payload = json.dumps({"title": title, "body": body, "url": URL})
    return subprocess.CompletedProcess(["gh"], 0, stdout=payload, stderr="")


class _Calls:
    """A recording stand-in for ``subprocess_utils.run``."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        self.commands.append(cmd)
        result = self._results[min(len(self.commands) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def _reads(monkeypatch: pytest.MonkeyPatch, *results: Any) -> _Calls:
    calls = _Calls(*results)
    monkeypatch.setattr(desc, "run", calls)
    return calls


# --------------------------------------------------------------------------- #
# The read itself.
# --------------------------------------------------------------------------- #


def test_a_github_read_asks_only_for_the_fields_the_panel_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repo is the server's own; the id travels as the issue number."""
    calls = _reads(monkeypatch, _gh())
    item_description(ITEM, config=_config(), now=MOMENT)
    assert calls.commands == [
        [
            "gh",
            "issue",
            "view",
            ITEM,
            "--repo",
            "kew-dev/kew",
            "--json",
            "title,body,url",
        ]
    ]


def test_the_body_is_split_into_description_expected_and_actual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads(monkeypatch, _gh())
    dto = item_description(ITEM, config=_config(), now=MOMENT)
    assert dto.availability == "available"
    assert dto.provenance == "observed"
    assert dto.title == "The Work screen has no description"
    assert dto.url == URL
    assert dto.expected.startswith("The issue body, split into its sections.")
    # A subsection of Expected belongs to Expected, not to the next section.
    assert "Headings below the section heading stay inside it." in dto.expected
    assert dto.actual == "An empty panel."
    # Everything else keeps its own heading, in the order it was written.
    assert dto.description.startswith("The Work screen shows no description at all.")
    assert "## Notes" in dto.description
    assert "## Expected" not in dto.description
    assert dto.reason == ""
    assert dto.observed_at == MOMENT.isoformat()
    assert dto.composed_at == MOMENT.isoformat()


def test_a_body_with_no_section_headings_is_all_description() -> None:
    body = "Just prose.\n\n## Background\n\nMore prose."
    description, expected, actual = split_body(body)
    assert description == body.strip()
    assert (expected, actual) == ("", "")


def test_a_hash_inside_a_fenced_block_is_code_not_a_heading() -> None:
    body = "Intro\n\n```sh\n# Expected\nkew serve\n```\n\n## Actual\n\nIt crashed."
    description, expected, actual = split_body(body)
    assert expected == ""
    assert "# Expected" in description
    assert "kew serve" in description
    assert actual == "It crashed."


def test_a_decorated_heading_still_names_its_section() -> None:
    body = "Intro\n\n## **Expected behaviour:**\n\nGreen.\n\n## Actual result\n\nRed."
    description, expected, actual = split_body(body)
    assert (description, expected, actual) == ("Intro", "Green.", "Red.")


def test_an_empty_body_read_successfully_is_available_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case where an empty description is the truth: a read that found
    nothing. It says ``available``, which is what makes every other empty
    answer readable as a failure."""
    _reads(monkeypatch, _gh(body=""))
    dto = item_description(ITEM, config=_config(), now=MOMENT)
    assert (dto.availability, dto.description, dto.reason) == ("available", "", "")


def test_a_very_long_body_is_clipped_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads(monkeypatch, _gh(body="x" * (MAX_BODY_CHARS * 2)))
    dto = item_description(ITEM, config=_config(), now=MOMENT)
    assert len(dto.description) == MAX_BODY_CHARS
    assert dto.description.endswith("…")


# --------------------------------------------------------------------------- #
# The cache.
# --------------------------------------------------------------------------- #


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    ticks = [1000.0]
    monkeypatch.setattr(desc, "_now", lambda: ticks[0])
    return ticks


def test_a_second_read_inside_the_ttl_does_not_touch_the_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh())
    first = item_description(ITEM, config=_config(), now=MOMENT)
    ticks[0] += DESCRIPTION_TTL_S - 1
    second = item_description(ITEM, config=_config(), now=MOMENT)
    assert len(calls.commands) == 1
    assert second.provenance == "cached"
    assert second.availability == "available"
    # The cached answer is the same answer, including when it was observed.
    assert second.description == first.description
    assert second.observed_at == first.observed_at


def test_past_the_ttl_the_tracker_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh(), _gh(body="Rewritten."))
    item_description(ITEM, config=_config(), now=MOMENT)
    ticks[0] += DESCRIPTION_TTL_S
    later = item_description(ITEM, config=_config(), now=MOMENT)
    assert len(calls.commands) == 2
    assert (later.provenance, later.description) == ("observed", "Rewritten.")


def test_two_items_do_not_share_one_cached_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh(), _gh(body="A different issue."))
    item_description(ITEM, config=_config(), now=MOMENT)
    other = item_description("2056", config=_config(), now=MOMENT)
    assert len(calls.commands) == 2
    assert other.description == "A different issue."


def test_two_repositories_do_not_cross_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """One process can serve two checkouts; the cache is partitioned by source."""
    _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh(), _gh(body="Another repo's issue."))
    item_description(ITEM, config=_config(), now=MOMENT)
    elsewhere = item_description(
        ITEM, config=_config(project=ProjectConfig(repo="kew-dev/other")), now=MOMENT
    )
    assert len(calls.commands) == 2
    assert elsewhere.description == "Another repo's issue."


def test_the_cache_cannot_grow_without_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _gh())
    for n in range(desc.CACHE_MAX_ENTRIES + 5):
        # A window apart, because walking this many novel ids back to back is
        # exactly what the read budget refuses; that bound has its own tests
        # below, and this one is about the cache.
        ticks[0] += READ_BUDGET_WINDOW_S
        item_description(str(n), config=_config(), now=MOMENT)
    assert len(desc._cache) == desc.CACHE_MAX_ENTRIES
    # The oldest readings are the ones evicted.
    assert ("github", "github:kew-dev/kew", "0") not in desc._cache


# --------------------------------------------------------------------------- #
# Unavailability — a reason, never a silent empty body.
# --------------------------------------------------------------------------- #


def _failed(stderr: str = "HTTP 503") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], 1, stdout="", stderr=stderr)


def test_a_tracker_failure_is_unavailable_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads(monkeypatch, _failed())
    dto = item_description(ITEM, config=_config(), now=MOMENT)
    assert dto.availability == "unavailable"
    assert dto.provenance == "error"
    assert "HTTP 503" in dto.reason
    assert (dto.description, dto.expected, dto.actual) == ("", "", "")
    assert dto.observed_at is None
    assert dto.title == ""


def test_a_tracker_timeout_is_unavailable_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads(monkeypatch, subprocess.TimeoutExpired(["gh"], desc.READ_TIMEOUT_S))
    dto = item_description(ITEM, config=_config(), now=MOMENT)
    assert dto.availability == "unavailable"
    assert dto.reason != ""


def test_an_unparseable_response_is_unavailable_not_an_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads(
        monkeypatch, subprocess.CompletedProcess(["gh"], 0, stdout="<html>", stderr="")
    )
    dto = item_description(ITEM, config=_config(), now=MOMENT)
    assert dto.availability == "unavailable"
    assert dto.reason != ""


def test_a_failed_refresh_serves_the_last_reading_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old description beats none — as long as it says it is old, and why."""
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _gh(), _failed("gh: connection refused"))
    fresh = item_description(ITEM, config=_config(), now=MOMENT)
    ticks[0] += DESCRIPTION_TTL_S
    stale = item_description(ITEM, config=_config(), now=MOMENT)
    assert stale.availability == "stale"
    assert stale.provenance == "cached"
    assert "connection refused" in stale.reason
    assert stale.description == fresh.description
    assert stale.observed_at == fresh.observed_at


def test_an_unconfigured_repo_is_unavailable_and_reads_no_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _reads(monkeypatch, _gh())
    dto = item_description(ITEM, config=_config(project=ProjectConfig()), now=MOMENT)
    assert (dto.availability, dto.reason) == ("unavailable", NO_REPO_REASON)
    assert calls.commands == []


def test_a_non_numeric_id_under_github_is_unavailable_not_a_shell_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id is opaque everywhere else; here the provider requires a number,
    so a Linear key under a GitHub tracker is refused rather than passed on."""
    calls = _reads(monkeypatch, _gh())
    dto = item_description("KEW-31", config=_config(), now=MOMENT)
    assert dto.availability == "unavailable"
    assert "KEW-31" in dto.reason
    assert calls.commands == []


def test_a_digit_that_is_not_an_ascii_digit_is_not_an_issue_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``"٢".isdigit()`` is true, and it is still not an issue reference."""
    calls = _reads(monkeypatch, _gh())
    dto = item_description("٢٠٥٥", config=_config(), now=MOMENT)
    assert dto.availability == "unavailable"
    assert calls.commands == []


def test_a_failure_is_not_cached_as_a_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _failed(), _gh())
    assert item_description(ITEM, config=_config(), now=MOMENT).availability == (
        "unavailable"
    )
    ticks[0] += 1
    assert item_description(ITEM, config=_config(), now=MOMENT).availability == (
        "available"
    )


# --------------------------------------------------------------------------- #
# Linear.
# --------------------------------------------------------------------------- #


def _linear_config() -> DispatchConfig:
    return DispatchConfig(
        project=ProjectConfig(repo="kew-dev/kew"),
        tracker=TrackerConfig(type="linear"),
    )


def test_a_linear_item_is_read_through_the_linear_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Tracker:
        def __init__(self, config: Any, client: Any) -> None:
            pass

        def fetch_issue(self, item_id: str) -> dict[str, Any]:
            assert item_id == "KEW-31"
            return {"title": "T", "body": "Prose.\n\n## Actual\n\nNothing.", "url": "u"}

    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setattr(desc, "LinearClient", lambda key, **budget: object())
    monkeypatch.setattr(desc, "LinearTracker", _Tracker)
    dto = item_description("KEW-31", config=_linear_config(), now=MOMENT)
    assert (dto.availability, dto.description, dto.actual) == (
        "available",
        "Prose.",
        "Nothing.",
    )


def test_linear_without_an_api_key_is_unavailable_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    dto = item_description("KEW-31", config=_linear_config(), now=MOMENT)
    assert (dto.availability, dto.reason) == ("unavailable", NO_LINEAR_KEY_REASON)


def test_a_linear_api_error_is_unavailable_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.linear.client import LinearApiError

    class _Tracker:
        def __init__(self, config: Any, client: Any) -> None:
            pass

        def fetch_issue(self, item_id: str) -> dict[str, Any]:
            raise LinearApiError("issue not found")

    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setattr(desc, "LinearClient", lambda key, **budget: object())
    monkeypatch.setattr(desc, "LinearTracker", _Tracker)
    dto = item_description("KEW-31", config=_linear_config(), now=MOMENT)
    assert dto.availability == "unavailable"
    assert "issue not found" in dto.reason


def test_a_linear_read_is_one_attempt_inside_the_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client's defaults are the epic driver's: three attempts, a 30s socket
    and 1s+2s of backoff — ~93s that a browser would spend waiting. This read
    takes one attempt on its own ``READ_TIMEOUT_S`` and answers with the reason.

    Driven through the real client and the real tracker, with only the socket
    seam replaced, so the budget is read off the transport rather than asserted
    against the call this module happens to make today.
    """
    from kew.linear import client as linear_client

    budgets: list[float | None] = []

    def _times_out(
        url: str, payload: bytes, headers: dict[str, str], **budget: float
    ) -> tuple[int, bytes]:
        # ``**budget`` rather than a named argument: a transport called without
        # a timeout at all is exactly the regression, and records as ``None``.
        budgets.append(budget.get("timeout"))
        raise TimeoutError("the read timed out")

    monkeypatch.setattr(linear_client, "_default_transport", _times_out)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    started = time.monotonic()
    dto = item_description("KEW-31", config=_linear_config(), now=MOMENT)
    elapsed = time.monotonic() - started
    assert dto.availability == "unavailable"
    assert dto.reason != ""
    # One attempt, bounded by this module's timeout — and so, no backoff.
    assert budgets == [desc.READ_TIMEOUT_S]
    assert elapsed < 1.0


# --------------------------------------------------------------------------- #
# The in-flight bound.
# --------------------------------------------------------------------------- #


class _Poll(threading.Thread):
    """One ``item_description`` call on its own thread, its answer captured."""

    def __init__(self, item_id: str, finished: threading.Semaphore) -> None:
        super().__init__(daemon=True)
        self.item_id = item_id
        self.dto: Any = None
        self._finished = finished

    def run(self) -> None:
        try:
            self.dto = item_description(self.item_id, config=_config(), now=MOMENT)
        finally:
            # Released only once ``dto`` is stored, so a waiter counting
            # finishes can read every answer it counted.
            self._finished.release()


class _Blocked:
    """A ``run`` that parks inside the tracker read until it is released.

    It parks only the ids :meth:`park` was given; a read of any OTHER id runs
    straight through, which is how a test asks whether the bound the parked reads
    are holding is still open for somebody else.
    """

    def __init__(self) -> None:
        self.entered = threading.Semaphore(0)
        self.finished = threading.Semaphore(0)
        self.release = threading.Event()
        self.threads: list[_Poll] = []
        self._parked: set[str] = set()

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        if cmd[3] not in self._parked:
            return _gh()
        self.entered.release()
        assert self.release.wait(10)
        return _gh()

    def park(
        self, monkeypatch: pytest.MonkeyPatch, count: int, *, item_id: str | None = None
    ) -> list[_Poll]:
        """Start ``count`` reads, returning once ONE of them is in the tracker.

        Without ``item_id`` each read is of a distinct cold id, so all ``count``
        of them park inside the tracker. With it, every read is of that SAME id,
        so only the one that wins the per-key gate gets that far — and what the
        others do instead is the thing under test.
        """
        self._parked = {item_id} if item_id else {f"90{n}" for n in range(count)}
        monkeypatch.setattr(desc, "run", self)
        self.threads = [_Poll(item_id or f"90{n}", self.finished) for n in range(count)]
        for thread in self.threads:
            thread.start()
        for _ in range(1 if item_id else count):
            assert self.entered.acquire(timeout=10)
        return self.threads

    def finish(self) -> None:
        """Let the parked reads complete, so no slot outlives the test."""
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=10)
            assert not thread.is_alive()


@pytest.fixture
def blocked() -> Any:
    """Parked reads, always released: a held slot would leak into the next test."""
    scene = _Blocked()
    try:
        yield scene
    finally:
        scene.finish()


def test_a_burst_of_cold_ids_cannot_hold_more_threads_than_the_bound(
    blocked: _Blocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache cannot bound this: the id comes from the client, so every novel
    id is a cold miss on a synchronous route. Past the bound the read is refused
    at once — it does not queue behind the tracker holding a worker thread."""
    blocked.park(monkeypatch, MAX_CONCURRENT_READS)
    started = time.monotonic()
    dto = item_description("9999", config=_config(), now=MOMENT)
    elapsed = time.monotonic() - started
    assert dto.availability == "unavailable"
    assert dto.reason == BUSY_REASON.format(limit=MAX_CONCURRENT_READS)
    assert elapsed < 1.0  # refused, not queued behind the parked reads


def test_a_refused_read_still_serves_a_reading_already_held(
    blocked: _Blocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is a read policy, not a new failure mode: over a cached body it
    answers ``stale`` with the reason, like every other failed refresh."""
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _gh())
    fresh = item_description(ITEM, config=_config(), now=MOMENT)
    ticks[0] += DESCRIPTION_TTL_S
    blocked.park(monkeypatch, MAX_CONCURRENT_READS)
    stale = item_description(ITEM, config=_config(), now=MOMENT)
    assert stale.availability == "stale"
    assert stale.description == fresh.description
    assert str(MAX_CONCURRENT_READS) in stale.reason


def test_the_bound_is_released_so_a_later_read_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that fails must give its slot back, or the bound leaks shut.

    Spends the WHOLE bound on failures before asking for one more read, because
    a single failure proves nothing: a release-on-success-only slip leaks one
    slot of four, and the reads after it are sequential, so they would each find
    a free slot anyway and this would stay green on borrowed state from the rest
    of the file. ``MAX_CONCURRENT_READS`` failures leak every slot, so the read
    after them is refused — which makes this red on its own.
    """
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, *([_failed()] * MAX_CONCURRENT_READS), _gh())
    for n in range(MAX_CONCURRENT_READS):
        ticks[0] += 1
        spent = item_description(f"70{n}", config=_config(), now=MOMENT)
        assert spent.availability == "unavailable"
    ticks[0] += 1
    dto = item_description("7999", config=_config(), now=MOMENT)
    assert dto.availability == "available"
    assert dto.reason == ""


def test_a_failed_read_releases_the_item_it_was_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-key gate is given back on failure too. Otherwise one failed read
    would make its item unreadable for the life of the process — refused as a
    duplicate of a read that is long over — while every other item kept working.
    """
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _failed(), _gh())
    assert item_description(ITEM, config=_config(), now=MOMENT).reason != ""
    ticks[0] += 1
    retried = item_description(ITEM, config=_config(), now=MOMENT)
    assert retried.availability == "available"
    assert len(calls.commands) == 2  # the retry reached the tracker, not the gate


def test_polls_of_one_item_do_not_spend_the_whole_bound(
    blocked: _Blocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Work screen polls, so the same item id arrives again while its read is
    still out at the tracker. One read of an item is in flight at a time: the
    duplicates are refused without being attempted, so they neither multiply the
    load on a slow tracker nor spend on ONE item the bound every other item's
    cold or expired read has to come through."""
    polls = blocked.park(monkeypatch, MAX_CONCURRENT_READS, item_id=ITEM)
    for _ in range(MAX_CONCURRENT_READS - 1):
        assert blocked.finished.acquire(timeout=10)
    answered = [poll.dto for poll in polls if poll.dto is not None]
    assert len(answered) == MAX_CONCURRENT_READS - 1  # one read, not one per poll
    assert {dto.availability for dto in answered} == {"unavailable"}
    assert {dto.reason for dto in answered} == {SAME_ITEM_REASON}
    # And the slots the duplicates did not spend are still there for someone else.
    other = item_description("9999", config=_config(), now=MOMENT)
    assert other.availability == "available"


def test_a_duplicate_poll_still_serves_a_reading_already_held(
    blocked: _Blocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-key gate is a read policy, not a new failure mode: over a cached
    body a refused duplicate answers ``stale`` with its reason, like every other
    failed refresh — and the read that won is refreshing the entry both of them
    asked for."""
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _gh())
    fresh = item_description(ITEM, config=_config(), now=MOMENT)
    ticks[0] += DESCRIPTION_TTL_S
    blocked.park(monkeypatch, 1, item_id=ITEM)
    stale = item_description(ITEM, config=_config(), now=MOMENT)
    assert stale.availability == "stale"
    assert stale.reason == SAME_ITEM_REASON
    assert stale.description == fresh.description
    assert stale.observed_at == fresh.observed_at


# --------------------------------------------------------------------------- #
# The read budget — what a caller may spend over time, not just hold at once.
# --------------------------------------------------------------------------- #


def _walk(
    monkeypatch: pytest.MonkeyPatch, ticks: list[float], count: int, *, first: int = 0
) -> list[Any]:
    """``count`` reads of distinct novel ids, one second apart."""
    out = []
    for n in range(first, first + count):
        ticks[0] += 1
        out.append(item_description(f"8{n:04d}", config=_config(), now=MOMENT))
    return out


def test_a_caller_walking_novel_ids_cannot_spend_without_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-flight bound caps what one instant holds; it does not cap what a
    caller spends, because four slots recycled as fast as the tracker answers is
    an unbounded request rate against credentials the dispatch driver shares. The
    cache cannot cap it either: a novel id is a miss, and the id comes from the
    client. So the reads are rationed per window, and past the ration the
    tracker is not touched at all."""
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh())
    spent = _walk(monkeypatch, ticks, MAX_READS_PER_WINDOW)
    assert {dto.availability for dto in spent} == {"available"}
    assert len(calls.commands) == MAX_READS_PER_WINDOW

    ticks[0] += 1
    refused = item_description("89999", config=_config(), now=MOMENT)
    assert refused.availability == "unavailable"
    assert refused.reason == BUDGET_REASON.format(
        limit=MAX_READS_PER_WINDOW, window=READ_BUDGET_WINDOW_S
    )
    assert len(calls.commands) == MAX_READS_PER_WINDOW  # not attempted


def test_the_budget_refills_as_the_window_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ceiling, not a fuse: the reads of a window leave it with the window."""
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh())
    _walk(monkeypatch, ticks, MAX_READS_PER_WINDOW)
    ticks[0] += READ_BUDGET_WINDOW_S
    allowed = item_description("89999", config=_config(), now=MOMENT)
    assert allowed.availability == "available"
    assert len(calls.commands) == MAX_READS_PER_WINDOW + 1


def test_the_panel_poll_of_one_item_spends_no_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the Work screen actually does must not be what exhausts the ration.
    A poll inside the TTL is answered from the cache before any gate, so an
    operator reading one item pays one read per TTL, not one per poll."""
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh())
    for _ in range(MAX_READS_PER_WINDOW * 2):
        ticks[0] += 1
        assert item_description(ITEM, config=_config(), now=MOMENT).availability == (
            "available"
        )
    assert len(calls.commands) == 1
    # And the whole ration is still there for the items nobody has read yet.
    assert _walk(monkeypatch, ticks, MAX_READS_PER_WINDOW)[-1].reason == ""


def test_clearing_the_cache_does_not_refill_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the cheapest way past the ration would be to empty the cache —
    which is what a caller walking novel ids effectively does anyway."""
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _gh())
    _walk(monkeypatch, ticks, MAX_READS_PER_WINDOW)
    desc.clear_cache()
    ticks[0] += 1
    assert (
        str(MAX_READS_PER_WINDOW)
        in item_description("89999", config=_config(), now=MOMENT).reason
    )


def test_a_read_refused_for_budget_still_serves_a_reading_already_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like every other refused refresh: ``stale`` with the reason attached, so
    the ration never turns a held description into an empty panel."""
    ticks = _clock(monkeypatch)
    _reads(monkeypatch, _gh())
    fresh = item_description(ITEM, config=_config(), now=MOMENT)
    # Expire the entry first, then spend the window the refresh would come out
    # of — the TTL is longer than the window, so the order has to be this way.
    ticks[0] += DESCRIPTION_TTL_S
    _walk(monkeypatch, ticks, MAX_READS_PER_WINDOW)
    ticks[0] += 1
    stale = item_description(ITEM, config=_config(), now=MOMENT)
    assert stale.availability == "stale"
    assert stale.description == fresh.description
    assert str(MAX_READS_PER_WINDOW) in stale.reason


def test_a_duplicate_poll_spends_no_budget(
    blocked: _Blocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gates are ordered: the per-key gate turns a duplicate away BEFORE the
    budget is charged, so the polls of one item cannot spend the ration the
    items nobody has read yet still need."""
    blocked.park(monkeypatch, MAX_CONCURRENT_READS, item_id=ITEM)
    for _ in range(MAX_CONCURRENT_READS - 1):
        assert blocked.finished.acquire(timeout=10)
    assert len(desc._read_starts) == 1  # the read that won, and nothing else


def _unsupported_tracker_config() -> DispatchConfig:
    """A tracker this module has no read for. ``TrackerConfig.type`` is a
    ``Literal``, so the value is set past validation — the refusal it reaches is
    reachable from a newer config file read by an older kew."""
    config = _config()
    config.tracker.type = "jira"  # type: ignore[assignment]
    return config


@pytest.mark.parametrize(
    ("item_id", "config_of", "env_key"),
    [
        ("٢٠٥٥", _config, "lin_api_test"),  # a digit that is not an ASCII digit
        ("KEW-31", _config, "lin_api_test"),  # not an issue number at all
        (ITEM, lambda: _config(project=ProjectConfig()), "lin_api_test"),
        (ITEM, _unsupported_tracker_config, "lin_api_test"),
        ("KEW-31", _linear_config, None),  # linear with no key to read with
    ],
)
def test_a_refusal_that_reaches_no_tracker_charges_no_budget(
    monkeypatch: pytest.MonkeyPatch,
    item_id: str,
    config_of: Any,
    env_key: str | None,
) -> None:
    """The ledger records tracker requests. These refusals make none — they are
    settled before the gates — so a caller cannot charge the ration with them.

    Spends MORE than a whole window's worth of them, because one leak proves
    nothing: what the overcharge buys is a window in which every uncached read
    of every item is refused for budget, at no off-box cost to the caller.
    """
    if env_key is None:
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LINEAR_API_KEY", env_key)
    ticks = _clock(monkeypatch)
    calls = _reads(monkeypatch, _gh())
    for _ in range(MAX_READS_PER_WINDOW + 1):
        ticks[0] += 1
        refused = item_description(item_id, config=config_of(), now=MOMENT)
        assert refused.availability == "unavailable"
    assert calls.commands == []  # nothing reached the tracker
    assert len(desc._read_starts) == 0  # so nothing was charged for it
    # And the ration a real read needs is still whole.
    ticks[0] += 1
    assert item_description("89999", config=_config(), now=MOMENT).reason == ""


def test_a_read_refused_as_busy_charges_no_budget(
    blocked: _Blocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-flight bound is the last gate before the tracker, and a read it
    refuses never gets there. Charging it would make the cheaper bound a way to
    spend the scarcer one: four parked reads, and every arrival behind them
    burns the window's ration without a single request leaving the box."""
    parked = blocked.park(monkeypatch, MAX_CONCURRENT_READS)
    assert len(desc._read_starts) == MAX_CONCURRENT_READS  # the parked reads
    for n in range(MAX_READS_PER_WINDOW + 1):
        refused = item_description(f"95{n:03d}", config=_config(), now=MOMENT)
        assert refused.reason == BUSY_REASON.format(limit=MAX_CONCURRENT_READS)
    assert len(desc._read_starts) == MAX_CONCURRENT_READS  # and only those
    blocked.finish()
    for thread in parked:
        assert thread.dto.availability == "available"


# --------------------------------------------------------------------------- #
# The serve route.
# --------------------------------------------------------------------------- #


def _mk_app(tmp_path: Path, *, expose: bool = False) -> Any:
    db = tmp_path / "dispatch.db"
    SqliteRunStore(str(db)).close()
    return create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token="TESTTOKEN",  # noqa: S106 — test-only session token
        sessions=SessionStore(),
        expose=expose,
        config=_config(),
    )


def test_route_returns_the_split_description(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reads(monkeypatch, _gh())
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "item_id",
        "availability",
        "provenance",
        "observed_at",
        "title",
        "url",
        "description",
        "expected",
        "actual",
        "reason",
        "composed_at",
    }
    assert body["item_id"] == ITEM
    assert body["availability"] == "available"
    assert body["actual"] == "An empty panel."


def test_route_answers_two_hundred_when_the_tracker_is_down(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel needs the reason to render its fallback, so the failure is a
    payload, not a 5xx the client has to interpret."""
    _reads(monkeypatch, _failed())
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["availability"] == "unavailable"
    assert body["reason"] != ""
    assert body["description"] == ""


def test_route_carries_an_opaque_id_through_the_path(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reads(monkeypatch, _gh())
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    body = client.get("/api/work/KEW-31/description").json()
    assert body["item_id"] == "KEW-31"


def test_exposed_read_requires_a_session(tmp_path: Path, inbox_client: Any) -> None:
    client = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert client.get(PATH).status_code == 401


def test_non_loopback_read_requires_a_session(
    tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(tmp_path), loopback_peer=False)
    assert client.get(PATH).status_code == 401


def test_a_cross_origin_page_cannot_drive_the_tracker_read(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This read's cost lands off-box — a subprocess and the operator's GitHub
    rate budget, which the dispatch driver shares — so unlike its neighbours it
    must not be drivable by a page the operator merely visits. CORS would keep
    such a page from READING the answer; it does nothing about the spending, and
    a simple GET is not preflighted, so the guard is what refuses it.
    """
    calls = _reads(monkeypatch, _gh())
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.get(PATH, headers=evil).status_code == 403
    # A subresource on another site (an `<img src>`) carries no Origin at all.
    assert client.get(PATH, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert calls.commands == []  # nothing reached the tracker


def test_the_app_own_page_reads_unchanged(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stricter guard is aimed at a cross-site driver, not at the panel: the
    SPA's own fetch is same-origin, so the browser sends no ``Origin`` and says
    so in ``Sec-Fetch-Site``."""
    _reads(monkeypatch, _gh())
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    ours = {"Sec-Fetch-Site": "same-origin"}
    assert client.get(PATH, headers=ours).status_code == 200


def test_the_sibling_reads_keep_the_plain_read_guard(
    tmp_path: Path, inbox_client: Any
) -> None:
    """Only the remote read is gated this way. The pipeline read composes from
    the local store, so tightening the description route must not quietly change
    what the rest of the Work screen accepts."""
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.get(f"/api/work/{ITEM}/pipeline", headers=evil).status_code != 403
