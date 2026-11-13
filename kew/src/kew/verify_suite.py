"""Which suite a PR's merge verify runs: the full one, or its selection (#3202).

With ``[verify] suite = "tiered"`` a PR verify's two pytest phases run the
test files :mod:`kew.verify_selection` selects for its change, plus every
``policy`` test; every other gate runs as it always does. The default,
``"full"``, changes nothing.

The selection is computed here, by the operator's kew, from git objects: the
candidate's own code never decides what runs. Anything that puts the
selection's safety in doubt runs the full suite instead, and says why:

* the nightly, or a sharded verify, or ``kew verify --full``;
* main is red, or its health cannot be read;
* no green nightly of a commit on main is on record within
  :data:`FRESH_WITHIN` — the nightly is what catches, and pins to a PR, a
  failure a selection let through. A PR's full verify that landed unchanged
  still counts toward main's health, but it is no nightly: it does not keep
  the selection running while the nightly is stale or missing;
* the selection cannot be computed, or one of its full-suite rules fires.

The coverage floor reads the whole suite's coverage, so it runs only in full
mode; a selected verify says so in its output.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import main_health, verify_selection

if TYPE_CHECKING:
    from kew.run_store import RunStore

#: ``[verify] suite``: the whole suite, or the selection when it is safe.
SUITE_FULL = "full"
SUITE_TIERED = "tiered"

#: What a verify ran: :data:`MODE_FULL`, or :data:`MODE_SELECTED`.
MODE_FULL = main_health.SUITE_FULL
MODE_SELECTED = main_health.SUITE_SELECTED

#: How old the newest green nightly of main may be for a selection to run.
FRESH_WITHIN = main_health.STALE_AFTER

#: A selected file the job may pass to pytest as one plain word.
_TEST_FILE = re.compile(r"\Atests/[\w./-]+\.py\Z")

DEFAULT_REASON = '[verify] suite = "full"'


@dataclass(frozen=True)
class SuiteSelection:
    """What a verify's pytest phases run, and why."""

    mode: str = MODE_FULL
    reason: str = DEFAULT_REASON
    #: The selected test files, relative to the project directory.
    files: tuple[str, ...] = ()
    #: The selection's digest, as ``kew verify --explain-selection`` prints
    #: it, whenever one was computed — also when it forced the full suite.
    digest: str | None = None
    selector_version: int | None = None
    #: Whether ``[verify] suite`` asked for a selection at all.
    tiered: bool = False

    @property
    def selected(self) -> bool:
        return self.mode == MODE_SELECTED

    def payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "files": list(self.files),
            "digest": self.digest,
            "selector_version": self.selector_version,
        }

    def describe(self) -> str:
        """One line for an operator."""
        if self.selected:
            return (
                f"suite: selected, {len(self.files)} test files plus -m policy "
                f"({self.reason}); the coverage floor is not applied, it runs "
                "only on the full suite"
            )
        return f"suite: full ({self.reason})"


FULL_SUITE = SuiteSelection()


@dataclass(frozen=True)
class SuiteRequest:
    """What :func:`choose_suite` reads about one verify."""

    repo_root: Path
    base_ref: str
    base_sha: str
    ref_sha: str
    kind: str = main_health.PR_KIND
    shards: int = 1
    force_full: bool = False


def _full(reason: str, **found: Any) -> SuiteSelection:
    return SuiteSelection(mode=MODE_FULL, reason=reason, tiered=True, **found)


def choose_suite(
    suite: str,
    request: SuiteRequest,
    *,
    store: RunStore | None,
    now: datetime | None = None,
) -> SuiteSelection:
    """The suite this verify runs: :data:`FULL_SUITE` unless *suite* is tiered
    and nothing puts the selection's safety in doubt."""
    if suite != SUITE_TIERED:
        return FULL_SUITE
    doubt = selection_doubt(request, store=store, now=now)
    if doubt is not None:
        return _full(doubt)
    return _select(request)


def selection_doubt(
    request: SuiteRequest, *, store: RunStore | None, now: datetime | None = None
) -> str | None:
    """Why this verify may not run a selection at all, or ``None``.

    The run's own kind, main's health and the nightly's freshness: what any
    selection, of tests or of frontend checks (:mod:`kew.verify_frontend`),
    leans on.
    """
    at = now or datetime.now(UTC)
    for check in _CHECKS:
        doubt = check(request, store, at)
        if doubt is not None:
            return doubt
    return None


def _run_doubt(
    request: SuiteRequest, _store: RunStore | None, _now: datetime
) -> str | None:
    if request.kind != main_health.PR_KIND:
        return "the nightly verifies main in full"
    if request.force_full:
        return "--full was passed"
    if request.shards > 1:
        return "a sharded verify runs the full suite"
    return None


def _health_doubt(
    request: SuiteRequest, store: RunStore | None, now: datetime
) -> str | None:
    try:
        health = main_health.read_main_health(
            store, request.repo_root, base_ref=request.base_ref, now=now
        )
    except main_health.MainHealthError as exc:
        return f"main's health could not be read: {exc}"
    if health.red:
        return f"main is red at {health.sha}"
    return None


def _nightly_doubt(
    request: SuiteRequest, store: RunStore | None, now: datetime
) -> str | None:
    try:
        nightly = main_health.newest_green_nightly(
            store, request.repo_root, base_ref=request.base_ref
        )
    except main_health.MainHealthError as exc:
        return f"main's health could not be read: {exc}"
    hours = FRESH_WITHIN.total_seconds() / 3600
    if nightly is None:
        return (
            "the nightly is missing: no green nightly of a commit on main is on record"
        )
    sha, at = nightly
    if now - at > FRESH_WITHIN:
        return (
            f"the nightly is stale: no green nightly of a commit on main within "
            f"{hours:g} h; the newest, of {sha[:12]}, was recorded at "
            f"{at.isoformat()}"
        )
    return None


_CHECKS: tuple[Callable[[SuiteRequest, RunStore | None, datetime], str | None], ...] = (
    _run_doubt,
    _health_doubt,
    _nightly_doubt,
)


def _select(request: SuiteRequest) -> SuiteSelection:
    """The selection itself, or the full suite when it cannot be trusted."""
    root = request.repo_root
    try:
        gate_re = verify_selection.gate_pattern()
        inputs = verify_selection.read_inputs(
            root, request.base_sha, request.ref_sha, verify_selection.FactCache(root)
        )
        selection = verify_selection.select(inputs, gate_re=gate_re)
    # Fail closed: whatever stops the selection runs the full suite.
    except Exception as exc:  # noqa: BLE001
        return _full(f"the selection could not be computed: {exc}")
    found: dict[str, Any] = {
        "digest": selection.digest(),
        "selector_version": verify_selection.SELECTOR_VERSION,
    }
    if selection.full_rule is not None:
        return _full(f"{selection.full_rule}: {selection.full_path}", **found)
    files = tuple(path for path, _why in selection.selected)
    odd = next((path for path in files if not _TEST_FILE.match(path)), None)
    if odd is not None:
        return _full(f"a selected file is no plain test path: {odd!r}", **found)
    return SuiteSelection(
        mode=MODE_SELECTED,
        reason=(
            f"{len(files)} of {selection.total_files} test files, "
            f"{selection.selected_tests} of {selection.total_tests} tests"
        ),
        files=files,
        tiered=True,
        **found,
    )
