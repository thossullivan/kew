"""Factory health: "is the factory able to work?", as one read-only reading (#2298).

The page this composes has the grouping of Dagster's Settings tabs — runtimes and
images, per-lane posture, runners and credentials, trackers, daemons,
configuration — in kew's own vocabulary. Nothing here is ported: the grouping is
an idea, and the facts are kew's.

Three rules shape every field below.

**Names, never values.** A credential is ``{name, set}``; an environment
variable is ``{name, purpose, required_by, set}``. No value of any environment
variable reaches this payload, which is pinned by a test that exports a sentinel
secret and greps the whole response.

**Absence is not failure.** Every reading is one of three states, not two:
running; not running but required; not enabled. A watcher that is off by design
is reported as not enabled and never as a fault, and a fact that cannot be
observed at all is ``None`` — never ``False``, which would read as a proven
negative.

**No GitHub call per request.** The tracker reading is the one the serve sweep
already cached in the ledger (``github_rate_limit``), so this endpoint is free
and offline-safe. The probes that touch the host — the container runtime, the
image list — are behind :class:`ProbeCache`, so a page refresh does not re-probe
and the sweep can warm them instead.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from kew.drive_heartbeat import DriveHeartbeat, read_drive_heartbeat
from kew.epic_archive import archived_epic_refs, epic_is_retired
from kew.runners.registry import runner_required_credentials
from kew.runtime_backend import (
    BACKED_RUNTIMES,
    DOCKER_COMPATIBLE_RUNTIMES,
    SUPPORTED_RUNTIMES,
    backend_for,
)
from kew.sandbox import (
    DEFAULT_IMAGE_RUNNER,
    REFUSAL_CREDENTIAL_MISSING,
    SandboxImageUnmapped,
    SandboxRuntimeUnavailable,
    posture_network_boundary,
    resolve_effective_sandbox,
    resolve_runner_image,
)
from kew.service.epics import (
    DRIVER_BUSY,
    DRIVER_LIVE,
    DRIVER_WAITING,
    DriverLockFacts,
    DriverWaiting,
    liveness_from_state,
    read_driver_waiting,
    reconciled_epic_snapshots,
    waiting_is_fresh,
)

if TYPE_CHECKING:
    from kew.config import DispatchConfig, SandboxConfig

_logger = logging.getLogger(__name__)

#: The window the observed-runtime counts are taken over. A week is long enough
#: to include a project that dispatches a few times a day and short enough that
#: a posture changed on Monday is visible by Friday.
OBSERVED_WINDOW_DAYS = 7

#: How long a host probe (runtime binary, image presence) is reused. The serve
#: sweep runs every 60s and warms the same cache, so an open health page costs
#: nothing; a page opened on a serve with no sweep probes at most once a minute.
PROBE_TTL_S = 60.0

#: The dispatch lanes a routing table defines, in the order the page lists them.
ROUTING_TIERS = ("simple", "medium", "complex")

#: The environment variables the factory reads, with what each is for and who
#: needs it. NAMES ONLY — this table exists so the page can say "the serve
#: process cannot see GH_TOKEN" without ever reading one.
#:
#: ``required_by`` is prose on purpose: "which runner needs which credential" is
#: the runners' own declaration (``required_credentials``), reported per runner
#: in ``runners[]``, and restating it here would be a second table to drift.
ENV_SURFACE: tuple[tuple[str, str, str], ...] = (
    ("GITHUB_TOKEN", "GitHub API and git push", "every repo read and publication"),
    ("GH_TOKEN", "GitHub API (gh CLI's own name)", "the gh CLI"),
    ("LINEAR_API_KEY", "Linear GraphQL API", "a Linear-tracked project"),
    ("ANTHROPIC_API_KEY", "Anthropic API account", "claude-code, API billing"),
    (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "Claude subscription login, forwardable to a container",
        "claude-code, subscription billing",
    ),
    ("OPENROUTER_API_KEY", "OpenRouter inference", "opencode, pi, hermes, lemonade"),
    ("KEW_PROJECT", "which project directory kew resolves against", "every command"),
    ("KEW_REPO", "the delivery repository override", "an operator override"),
    ("KEW_LOG_DIR", "where the ledger lives", "an operator override"),
)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _age_s(stamp: str | None, now: datetime) -> float | None:
    """Seconds since an ISO stamp, or None when there is none to read.

    An unreadable stamp ages to None — "we cannot age this" — never 0, which
    would read as "written just now". A naive stamp is read as UTC, the only
    timezone any kew writer stamps in. Tolerated, never repaired.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        _logger.warning("health: stamp %r is unreadable; age reads unknown", stamp)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed).total_seconds()


# ── probes ──────────────────────────────────────────────────────────────────


def _probe_runtime(runtime: str) -> bool | None:
    """Is ``runtime`` usable on this host? None when this build has no backend.

    Both halves go through the runtime seam (#2314) rather than reaching for
    ``shutil.which`` here: the registry says which runtimes are backed, and the
    backend says whether this host has one. ``openshell`` appeared in
    ``runtimes[]`` as a named, unbacked runtime until its backend landed (epic
    node N8) and became a real probe without this module learning its name.

    The membership test comes FIRST, so "no backend" is a reading rather than a
    refusal swallowed off ``backend_for``: ``None`` means "no backend here", and
    the row carries ``backed: false`` as the reason beside it.
    """
    if runtime not in BACKED_RUNTIMES:
        return None
    return backend_for(runtime).probe() is not None


def _probe_image(runtime: str, ref: str) -> bool | None:
    """Is image ``ref`` present locally? None when the question cannot be put.

    The slow probe of the set, which is why it sits behind the cache. A runtime
    this build cannot drive, or one that is not installed, cannot answer at all —
    that is unknown, not absent, because reporting "missing" for an image on a
    host with no docker would paint a red × over a question nobody asked.

    A wedged daemon is the same reading. ``run`` raises
    :class:`subprocess.TimeoutExpired` when the 15s bound expires, and that is a
    :class:`subprocess.SubprocessError` rather than an ``OSError`` — so it is
    caught here with the rest. A docker that never answers must leave this row
    unknown, not take the whole ``/api/health`` response down with it.
    """
    if runtime not in DOCKER_COMPATIBLE_RUNTIMES:
        return None
    from kew.subprocess_utils import run

    try:
        result = run([runtime, "image", "inspect", ref], check=False, timeout=15)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _logger.warning("health: image probe for %r failed: %s", ref, exc)
        return None
    return result.returncode == 0


def _probe_env(name: str) -> bool:
    """Is ``name`` set to something non-empty in THIS process's environment?

    The only thing ever asked of an environment variable here. The answer is a
    boolean; the value is not read, returned, logged or compared.
    """
    return bool(os.environ.get(name))


@dataclass(frozen=True)
class HealthProbes:
    """The host questions this reading asks, injected so they can be faked.

    Tests pass fakes for all three (docker, gh, env); production takes the
    defaults. Each returns a plain value, never a subprocess result, so the
    assembly below cannot accidentally come to depend on a probe's internals.
    """

    runtime_present: Callable[[str], bool | None] = _probe_runtime
    image_present: Callable[[str, str], bool | None] = _probe_image
    env_set: Callable[[str], bool] = _probe_env


class ProbeCache:
    """TTL cache in front of the host probes, with one lock per question.

    The health endpoint must not shell out once per request: a tab left open on
    ``/health`` would run ``docker image inspect`` for every image every few
    seconds. Entries are refreshed on a miss and by
    :meth:`warm`, which the serve sweep calls — so on a live serve the page
    reads a value somebody else paid for.
    """

    def __init__(self, probes: HealthProbes, *, ttl_s: float = PROBE_TTL_S) -> None:
        self._probes = probes
        self._ttl_s = ttl_s
        self._values: dict[tuple[str, ...], tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _cached[T](self, key: tuple[str, ...], read: Callable[[], T]) -> T:
        with self._lock:
            hit = self._values.get(key)
            if hit is not None and time.monotonic() - hit[0] < self._ttl_s:
                return cast("T", hit[1])
        value = read()
        with self._lock:
            self._values[key] = (time.monotonic(), value)
        return value

    def runtime_present(self, runtime: str) -> bool | None:
        return self._cached(
            ("runtime", runtime), lambda: self._probes.runtime_present(runtime)
        )

    def image_present(self, runtime: str, ref: str) -> bool | None:
        return self._cached(
            ("image", runtime, ref), lambda: self._probes.image_present(runtime, ref)
        )

    def env_set(self, name: str) -> bool:
        # Not cached: reading one environment variable's presence costs nothing,
        # and a stale "not set" would outlive the restart that fixed it.
        return self._probes.env_set(name)

    def warm(self, config: DispatchConfig) -> None:
        """Refresh every probe this config's lanes will be asked about."""
        sandbox = config.sandbox
        for runtime in SUPPORTED_RUNTIMES:
            self._values.pop(("runtime", runtime), None)
            self.runtime_present(runtime)
        for entry in _image_entries(sandbox, _lane_runners(config)):
            if entry["ref"] is None:
                continue
            self._values.pop(("image", sandbox.runtime, entry["ref"]), None)
            self.image_present(sandbox.runtime, entry["ref"])


# ── lanes, images, posture ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Lane:
    """One place a dispatch can run: a routing tier, or the review stage."""

    name: str
    runner: str


def review_lane_runner(config: DispatchConfig) -> str:
    """The runner the reviewer runs as.

    Mirrors ``kew.graph.driver.review_launcher.review_runner``, which the
    service layer may not import (``kew.service`` is the far side of the
    ``kew.graph`` boundary). ``tests/test_service_health.py`` holds the two to
    each other, so the mirror cannot drift into a second answer.
    """
    return config.review.runner or DEFAULT_IMAGE_RUNNER


def _lanes(config: DispatchConfig) -> list[Lane]:
    """Every lane, deduped by name, in the order the page lists them."""
    from kew.runners.registry import resolve_runner

    lanes: list[Lane] = []
    tiers: list[str | None] = [None]
    if config.routing is not None:
        tiers += list(ROUTING_TIERS)
    for tier in tiers:
        try:
            runner = resolve_runner(config, tier).runner.name
        except (ValueError, KeyError):
            continue
        lanes.append(Lane(name=tier or "default", runner=runner))
    lanes.append(Lane(name="review", runner=review_lane_runner(config)))
    return lanes


def _lane_runners(config: DispatchConfig) -> set[str]:
    return {lane.runner for lane in _lanes(config)}


def _image_entry(
    sandbox: SandboxConfig, runner: str, *, routed: bool
) -> dict[str, Any]:
    """One row of ``images[]``: which image runs this runner, and where from.

    The ref comes from ``resolve_runner_image`` — the same resolution dispatch
    and the reviewer launcher both use — so the page cannot name an image a
    launch would not pick. Its refusal is the honest ``unmapped`` reading, which
    is a real configuration state and not an error to raise at a reader.
    """
    try:
        ref: str | None = resolve_runner_image(sandbox, runner)
    except SandboxImageUnmapped:
        return {"runner": runner, "ref": None, "source": "unmapped", "routed": routed}
    source = f"images.{runner}" if sandbox.images.get(runner) else "image"
    return {"runner": runner, "ref": ref, "source": source, "routed": routed}


def _image_entries(sandbox: SandboxConfig, routed: set[str]) -> list[dict[str, Any]]:
    """The images worth listing: every routed runner, plus every mapped one.

    A mapped image no lane routes to is listed and marked ``routed: false``,
    because an operator who mapped it wants to see it — and it is exactly the
    row the header must NOT count when it is missing.
    """
    runners = set(routed) | set(sandbox.images)
    if sandbox.image:
        runners.add(DEFAULT_IMAGE_RUNNER)
    return [
        _image_entry(sandbox, runner, routed=runner in routed)
        for runner in sorted(runners)
    ]


def _credential_mode(runner: str, cache: ProbeCache) -> str:
    """``real`` when the runner has an in-container credential it can use here.

    ``placeholder`` is the other reading: the runner declares credential names
    and this process has none of them set, so a contained launch would
    authenticate as nothing. Truthiness is all that is read — never the value.
    """
    names = runner_required_credentials(runner)
    if any(cache.env_set(name) for name in names):
        return "real"
    return "placeholder"


def _lane_posture(
    lane: Lane, config: DispatchConfig, cache: ProbeCache
) -> dict[str, Any]:
    """This lane's containment posture, resolved by the dispatch code itself.

    ``resolve_effective_sandbox`` is the one site that decides containment, and
    it is asked here rather than re-derived: a health page that computed its own
    answer could call a lane contained that dispatch would refuse. Its refusals
    are readings too — a refusal means this lane cannot run at all — so each is
    reported with the reason it carried, and nothing is raised at a reader.
    """
    sandbox = config.sandbox
    entry: dict[str, Any] = {
        "lane": lane.name,
        "runner": lane.runner,
        "runtime": sandbox.runtime,
        "contained": False,
        "egress": "unavailable",
        "credential_mode": _credential_mode(lane.runner, cache),
        "dangerously_skip_containment": sandbox.dangerously_skip_containment,
        "posture": "refused",
        "refusal": None,
        # Why THIS console process could not launch the lane, when that is a
        # fact about the console's own environment rather than the lane's.
        "console_refusal": None,
    }
    try:
        decision = resolve_effective_sandbox(sandbox, runner=lane.runner)
    except SandboxRuntimeUnavailable as exc:
        reason = getattr(exc, "reason", None) or "unavailable"
        if reason == REFUSAL_CREDENTIAL_MISSING:
            # The resolve read THIS process's environment, and the console is
            # not what dispatches: the loops and the daemon run from the
            # operator's shell, which holds the credential (#2534). So the
            # lane's posture is unknown from here, and the console's own
            # inability to launch is reported beside it, not as a refusal.
            entry["posture"] = "unknown"
            entry["console_refusal"] = reason
            return entry
        entry["refusal"] = reason
        return entry
    except ValueError as exc:  # a route that cannot resolve is not a crash here
        entry["refusal"] = str(exc)[:200]
        return entry
    entry["posture"] = decision.posture
    entry["contained"] = decision.posture != "uncontained"
    entry["runtime"] = decision.runtime or sandbox.runtime
    entry["egress"] = posture_network_boundary(
        decision.posture, decision.egress_verification
    )
    return entry


def _runtimes(config: DispatchConfig, cache: ProbeCache) -> list[dict[str, Any]]:
    """Every runtime kew has a NAME for — a list, so a new one simply appears.

    A name in ``SUPPORTED_RUNTIMES`` with no backend in this build is listed
    with ``backed: false`` and ``present: null``: named, honestly unusable, and
    never a red × for being unfinished. Every supported name is backed today,
    so the honest-unknown row is the shape the list keeps rather than one it
    currently shows.
    """
    return [
        {
            "name": runtime,
            "configured": runtime == config.sandbox.runtime,
            "backed": runtime in BACKED_RUNTIMES,
            "present": cache.runtime_present(runtime),
        }
        for runtime in SUPPORTED_RUNTIMES
    ]


def _images(config: DispatchConfig, cache: ProbeCache) -> list[dict[str, Any]]:
    runtime = config.sandbox.runtime
    entries = _image_entries(config.sandbox, _lane_runners(config))
    for entry in entries:
        ref = entry["ref"]
        entry["present"] = None if ref is None else cache.image_present(runtime, ref)
    return entries


#: What the runner row says when its credential is missing from THIS process
#: only (#2566): the loops dispatch from the operator's shell, which holds it.
SERVE_ONLY_CREDENTIAL_NOTE = "not visible to kew serve; the loops hold their own"


def _credential_note(credentials: list[dict[str, Any]], succeeded: int) -> str | None:
    """The quiet note for a credential only ``kew serve`` lacks, or None.

    ``kew serve`` reads its OWN environment, and it is not what dispatches. A
    runner that declares credentials, has none of them set here, and still
    finished a run successfully inside the observed window is proof that the
    processes which DO dispatch hold one — so the gap is a note, not an outage.
    With no such run there is no proof, and the runner stays down.
    """
    if not credentials or any(cred["set"] for cred in credentials):
        return None
    return SERVE_ONLY_CREDENTIAL_NOTE if succeeded > 0 else None


def _runner_row(
    runner: str,
    lane_names: list[str],
    config: DispatchConfig,
    cache: ProbeCache,
    succeeded: int = 0,
) -> dict[str, Any]:
    """One ``runners[]`` row: reachability plus the credential names it declares."""
    from kew.subprocess_utils import inspect_runner

    try:
        reach: Any = inspect_runner(config, runner_name=runner)
    except (ValueError, KeyError, OSError) as exc:
        _logger.warning("health: runner %r could not be inspected: %s", runner, exc)
        reach = None
    credentials = [
        {"name": name, "set": cache.env_set(name)}
        for name in runner_required_credentials(runner)
    ]
    return {
        "name": runner,
        "binary": None if reach is None else reach.binary,
        "binary_present": None if reach is None else reach.binary_present,
        "version": None if reach is None else reach.version,
        "version_supported": None if reach is None else reach.version_supported,
        "credential_names": credentials,
        "succeeded_in_window": succeeded,
        "credential_note": _credential_note(credentials, succeeded),
        "routed_tiers": [name for name in lane_names if name != "review"],
        "review_lane": "review" in lane_names,
    }


def _runners(
    config: DispatchConfig,
    cache: ProbeCache,
    succeeded: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Per-runner reachability, one row per runner any lane resolves to.

    The credential check is each runner's OWN ``required_credentials`` (#1146),
    not ``CLAUDE_AUTH_ENVS``: that tuple is claude-only and does not contain
    ``CLAUDE_CODE_OAUTH_TOKEN``, so an operator authenticated by the
    subscription path — the common one — was reported as having no credential.
    The ``[review]`` lane is included for the same reason: it has its own runner
    pin, and a reachability reading that skipped it said nothing about the stage
    that holds every PR.
    """
    lanes_by_runner: dict[str, list[str]] = {}
    for lane in _lanes(config):
        lanes_by_runner.setdefault(lane.runner, []).append(lane.name)
    return [
        _runner_row(runner, lane_names, config, cache, (succeeded or {}).get(runner, 0))
        for runner, lane_names in sorted(lanes_by_runner.items())
    ]


# ── trackers, daemons ───────────────────────────────────────────────────────


#: ``read_rate_limit`` reasons that mean "no answer came back at all" — gh could
#: not be run, or it ran and returned nothing. For a project whose work lives on
#: GitHub that is a real outage, not an unknown, so it reads as unreachable.
_GH_NO_ANSWER = frozenset({"unavailable"})


def _github_verdict(ok: bool, reason: str) -> tuple[bool | None, bool | None]:
    """``(reachable, auth_ok)`` from the cached reading's own two fields.

    Deliberately separate, because they are different repairs: a 401 is a
    credential to fix and an unreachable API is a network or a missing ``gh``.
    ``None`` is the third answer in both columns — a reading that answered with
    something kew cannot parse says nothing about the credential, and a reading
    nobody has taken says nothing about either.
    """
    if ok:
        return True, True
    if reason in _GH_NO_ANSWER:
        return False, None
    if reason == "unauthorized":
        return True, False
    # An http_4xx/5xx or an unparseable body: something answered, so the API is
    # reachable; whether the credential is good is not what it told us.
    return True, None


def _github_tracker(cached: str | None, now: datetime) -> dict[str, Any]:
    """The GitHub reading the sweep cached — no call of this endpoint's own.

    Both columns are ``None`` when no sweep has taken a reading here, because
    never having looked is not a failure. A reading that cannot be parsed is
    tolerated and reported as unparseable; it is never rewritten.
    """
    import json

    entry: dict[str, Any] = {
        "reachable": None,
        "auth_ok": None,
        "scopes": [],
        "rate": None,
        "observed_at": None,
        "observed_age_s": None,
        "reason": "",
    }
    if not cached:
        entry["reason"] = "no_reading"
        return entry
    try:
        reading = json.loads(cached)
    except ValueError:
        entry["reason"] = "unparseable"
        return entry
    if not isinstance(reading, dict):
        entry["reason"] = "unparseable"
        return entry
    reason = str(reading.get("reason") or "")
    reachable, auth_ok = _github_verdict(bool(reading.get("ok")), reason)
    entry.update(
        {
            "reachable": reachable,
            "auth_ok": auth_ok,
            "scopes": list(reading.get("scopes") or []),
            "rate": reading.get("core"),
            "observed_at": reading.get("observed_at"),
            "observed_age_s": _age_s(reading.get("observed_at"), now),
            "reason": reason,
        }
    )
    return entry


def _linear_tracker(config: DispatchConfig, cache: ProbeCache) -> dict[str, Any]:
    """Whether Linear is this project's tracker and whether its key is present.

    ``reachable`` is ``None``, always: nothing caches a Linear probe yet, and
    this endpoint makes no call. An unknown reading says so rather than
    guessing — and a project on GitHub is ``configured: false``, which the page
    draws grey, never red.
    """
    configured = config.tracker.type == "linear"
    return {
        "configured": configured,
        "credential_set": cache.env_set("LINEAR_API_KEY"),
        "reachable": None,
        "team": config.tracker.linear.team if configured else "",
    }


def _retired_epic_refs(store: Any, refs: list[str]) -> set[str]:
    """The refs among *refs* that are archived or have finished."""
    archived = archived_epic_refs(store)
    live = [ref for ref in refs if ref not in archived]
    snapshots = reconciled_epic_snapshots(
        store, {ref: store.latest_epic_tick(ref) for ref in live}
    )
    finished = {
        ref
        for ref, snapshot in snapshots.items()
        if epic_is_retired(snapshot.get("signal") if snapshot else None, archived=False)
    }
    return (set(refs) & archived) | finished


def _fresh_waiting(
    store: Any, ref: str, reading: Any, now: datetime
) -> DriverWaiting | None:
    """The waiting heartbeat that turns a silent driver into ``waiting`` (#2566).

    Only a driver that is NOT already ticking is re-read: ``live`` and ``busy``
    are stronger evidence than a loop's own word. And only inside twice the
    cadence, so a loop that said "waiting" and then died reads ``gone`` again
    one missed cadence later. A dead lock pid does not veto it: the lock's
    holder is the last tick's process, and a loop parked in a slot gate holds
    no lock — its fresh heartbeat is the only live evidence there is.
    """
    if reading.status in (DRIVER_LIVE, DRIVER_BUSY):
        return None
    waiting = read_driver_waiting(store, ref)
    return waiting if waiting_is_fresh(waiting, reading.interval_s, now) else None


def _driver_rows(
    store: Any, now: datetime, drive: DriveHeartbeat | None = None
) -> list[dict[str, Any]]:
    """Per-epic driver liveness, thresholded by the service's own reading.

    ``liveness`` is ``live``/``busy``/``gone``/``unknown`` from
    ``kew.service.epics.liveness_from_state`` — the same function the Epics list
    and an epic page read — so this page cannot paint a driver green that the
    rest of the console calls gone. A silent driver whose loop has said it is
    waiting on purpose reads ``waiting`` instead (:func:`_fresh_waiting`).

    ``active`` is false for an archived or complete epic (#2534): it has no
    driver left to lose, so its silence is history, not an outage, and the
    header must not count it. The predicate is ``epic_is_retired`` over the
    reconciled snapshot — the same one the needs-you set retires epics by.

    ``drive`` is the project drive's heartbeat: an epic in its rotation reads
    ``busy`` while it waits its turn or its detached review runs, and ``gone``
    only once the drive's own heartbeat stalls (#2617).
    """
    states = {ref: store.get_epic_driver_state(ref) for ref in store.list_epic_refs()}
    driven = sorted(ref for ref, state in states.items() if state is not None)
    retired = _retired_epic_refs(store, driven) if driven else set()
    return [
        _driver_row(
            store, ref, states[ref], active=ref not in retired, now=now, drive=drive
        )
        for ref in driven
    ]


def _driver_row(
    store: Any,
    ref: str,
    state: Any,
    *,
    active: bool,
    now: datetime,
    drive: DriveHeartbeat | None = None,
) -> dict[str, Any]:
    """One ``drivers[]`` row: the liveness reading, its waiting word, the lock."""
    reading = liveness_from_state(state, ref, now=now, drive=drive)
    lock = DriverLockFacts.from_state(state)
    waiting = _fresh_waiting(store, ref, reading, now)
    return {
        "epic_ref": ref,
        "active": active,
        "liveness": reading.status if waiting is None else DRIVER_WAITING,
        "waiting_reason": None if waiting is None else waiting.reason,
        "waiting_at": None if waiting is None else waiting.at,
        "tick_age_s": reading.age_s,
        "interval_s": reading.interval_s,
        "last_tick_at": reading.last_heartbeat_at,
        "drive": reading.drive,
        "lock": {
            "pid_alive": None if lock is None else lock.pid_alive,
            "heartbeat_age_s": None if lock is None else _age_s(lock.heartbeat_at, now),
            "observed_age_s": None if lock is None else _age_s(lock.observed_at, now),
        },
    }


def _drive_reading(
    drive: DriveHeartbeat | None, now: datetime
) -> dict[str, Any] | None:
    """The project drive's own heartbeat (#2617), or None when no drive holds
    the lock. ``status`` is ``gone`` once the heartbeat is older than its stall
    line (three of its write intervals), else ``live``."""
    if drive is None:
        return None
    return {
        "status": drive.status(now),
        "pid": drive.pid,
        "host": drive.host,
        "stage": drive.stage,
        "epic": drive.epic,
        "rotation": list(drive.rotation),
        "heartbeat_at": drive.at.isoformat(),
        "heartbeat_age_s": drive.age_s(now),
        "every_s": drive.every_s,
        "stall_after_s": drive.stall_after_s,
    }


def _watcher_rows(
    config: DispatchConfig, store: Any, now: datetime, epic_pollers: int | None
) -> list[dict[str, Any]]:
    """The watchers, each with whether it is ENABLED before whether it is running.

    Two states would be a lie here. ``kew delegate watch`` is off by design on a
    GitHub project and on a Linear project with no assignee configured, and a
    surface that only knows running/not-running paints that a failure. It also
    writes no heartbeat — only a cursor — so even when enabled its liveness is
    honestly ``None`` rather than a guess in either direction.
    """
    delegation_enabled = config.tracker.type == "linear" and bool(
        config.delegation.assignee
    )
    cursor = store.get_meta("delegation_watch_cursor") if delegation_enabled else None
    return [
        {
            "name": "delegation",
            "enabled": delegation_enabled,
            "running": None,
            "detail": "kew delegate watch — assign-to-kew trigger",
            "last_seen_at": cursor,
            "last_seen_age_s": _age_s(cursor, now),
        },
        {
            "name": "epic_stream",
            "enabled": True,
            # Zero live pollers is the normal state with no browser attached, so
            # this is not a fault either — the page says "no subscriber".
            "running": None if epic_pollers is None else epic_pollers > 0,
            "detail": "serve SSE fan-out, one poller per watched epic",
            "pollers": epic_pollers,
        },
    ]


def _loop_counts(store: Any) -> dict[str, int]:
    """Pursuing and stale loop counts, from the loops service's own reading."""
    from kew.service.loops import list_loops

    loops = list_loops(store)
    return {
        "pursuing": sum(1 for loop in loops if loop.status == "pursuing"),
        "stale": sum(1 for loop in loops if loop.stale),
    }


# ── the reading ─────────────────────────────────────────────────────────────


@dataclass
class HealthReader:
    """Holds the probe cache across requests, so a refresh re-probes nothing."""

    probes: HealthProbes = field(default_factory=HealthProbes)
    cache: ProbeCache | None = None

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = ProbeCache(self.probes)

    def warm(self, config: DispatchConfig) -> None:
        """Re-probe the host facts, so an open page reads a warm value.

        Called by the serve sweep. Fail-soft at the caller: a probe that cannot
        answer leaves the previous reading, and nothing here may break a cycle
        that also syncs outcomes.
        """
        assert self.cache is not None  # nosec B101 — set in __post_init__
        self.cache.warm(config)

    def read(
        self,
        store: Any,
        config: DispatchConfig,
        *,
        now: datetime | None = None,
        sweep: dict[str, Any] | None = None,
        epic_pollers: int | None = None,
    ) -> dict[str, Any]:
        return assemble_health(
            store,
            config,
            cache=self.cache,
            now=now,
            sweep=sweep,
            epic_pollers=epic_pollers,
        )


def assemble_health(
    store: Any,
    config: DispatchConfig,
    *,
    cache: ProbeCache | None = None,
    now: datetime | None = None,
    sweep: dict[str, Any] | None = None,
    epic_pollers: int | None = None,
) -> dict[str, Any]:
    """One health reading. Bounded, cached, and free of any GitHub call.

    ``checked_at`` is when THIS reading was assembled, which is what the
    refresh control's "data is at most N s old" is measured from. The host
    probes behind it may be up to :data:`PROBE_TTL_S` older, and say so in their
    own fields rather than pretending to the freshness of the envelope.
    """
    moment = now or datetime.now(UTC)
    probe_cache = cache if cache is not None else ProbeCache(HealthProbes())
    since = moment - timedelta(days=OBSERVED_WINDOW_DAYS)
    drive = read_drive_heartbeat(store)
    return {
        "checked_at": _iso(moment),
        "observed_window_days": OBSERVED_WINDOW_DAYS,
        "runtimes": _runtimes(config, probe_cache),
        "images": _images(config, probe_cache),
        "posture": [
            _lane_posture(lane, config, probe_cache) for lane in _lanes(config)
        ],
        "observed_7d": _observed_runtimes(store, since),
        "runners": _runners(config, probe_cache, _succeeded_runners(store, since)),
        "trackers": {
            "github": _github_tracker(_meta(store, "github_rate_limit"), moment),
            "linear": _linear_tracker(config, probe_cache),
        },
        "sweep": _sweep_reading(sweep, _meta(store, "scheduler_heartbeat"), moment),
        "drive": _drive_reading(drive, moment),
        "drivers": _driver_rows(store, moment, drive),
        "watchers": _watcher_rows(config, store, moment, epic_pollers),
        "loops": _loop_counts(store),
        "fleet": _fleet(store),
    }


def _meta(store: Any, key: str) -> str | None:
    try:
        value = store.get_meta(key)
    except (OSError, ValueError) as exc:
        _logger.warning("health: meta %r could not be read: %s", key, exc)
        return None
    return None if value is None else str(value)


def _observed_runtimes(store: Any, since: datetime) -> dict[str, int]:
    """What the fleet really ran inside over the window, counted SQL-side."""
    try:
        return dict(store.count_container_runtimes(since))
    except (AttributeError, OSError, ValueError) as exc:
        _logger.warning("health: observed runtime counts unavailable: %s", exc)
        return {}


def _succeeded_runners(store: Any, since: datetime) -> dict[str, int]:
    """Successful runs per runner over the window, counted SQL-side (#2566)."""
    try:
        return dict(store.count_successful_runs_by_runner(since))
    except (AttributeError, OSError, ValueError) as exc:
        _logger.warning("health: successful runs per runner unavailable: %s", exc)
        return {}


def _sweep_reading(
    sweep: dict[str, Any] | None, heartbeat: str | None, now: datetime
) -> dict[str, Any]:
    """The serve sweep's own liveness, plus the heartbeat it writes.

    ``present: false`` is a real reading — a ``kew serve`` started without the
    sweep (which every test does) is not a broken one — and the stored heartbeat
    is reported either way, because it is the only evidence a sweep ran here at
    all before this process started.
    """
    reading: dict[str, Any] = {
        "present": sweep is not None,
        "heartbeat_at": heartbeat,
        "heartbeat_age_s": _age_s(heartbeat, now),
    }
    if sweep is None:
        return reading
    reading.update(dict(sweep))
    reading["last_ok_age_s"] = _age_s(sweep.get("last_ok_at"), now)
    reading["last_attempt_age_s"] = _age_s(sweep.get("last_attempt_at"), now)
    # The cached rate-limit reading reaches the browser through `trackers`,
    # which strips it to the fields a page draws; the raw payload would carry
    # whatever a future writer puts in it, so it does not travel twice.
    reading.pop("rate_limit", None)
    return reading


def _fleet(store: Any) -> dict[str, Any]:
    """The dispatch latch and the queue depth behind it."""
    from kew.service.fleet import fleet_status

    status = fleet_status(store)
    stopped = store.get_fleet_stopped()
    return {
        "stopped": status.stopped,
        "pending": status.pending,
        "reason": (stopped or {}).get("reason", "") if stopped else "",
    }


def env_report(cache: ProbeCache | None = None) -> list[dict[str, Any]]:
    """``[{name, purpose, required_by, set}]`` — as seen by the serve process.

    Names only. ``set`` is the single bit read from each variable, and no code
    path here can return a value: the whole point of the endpoint is to let an
    operator answer "does the server see my token?" without printing it.
    """
    probe = cache if cache is not None else ProbeCache(HealthProbes())
    return [
        {
            "name": name,
            "purpose": purpose,
            "required_by": required_by,
            "set": probe.env_set(name),
        }
        for name, purpose, required_by in ENV_SURFACE
    ]


__all__ = [
    "ENV_SURFACE",
    "OBSERVED_WINDOW_DAYS",
    "PROBE_TTL_S",
    "HealthProbes",
    "HealthReader",
    "Lane",
    "ProbeCache",
    "SandboxImageUnmapped",
    "assemble_health",
    "env_report",
    "review_lane_runner",
]
