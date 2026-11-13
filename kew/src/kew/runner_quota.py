"""A runner whose provider account refused it for quota (#2643).

On 2026-10-09 the agent token's account hit its seven-day limit. Every run
after that failed in two seconds with zero tokens, each was recorded as an
ordinary failure, and the epic loops kept dispatching into the wall — every
node it touched then needed a manual ``kew epic retry``. The cause sat in the
transcript the whole time: a ``rate_limit_event`` with ``status=rejected``.

This module owns that fact end to end:

* :class:`QuotaRejection` is what a runner adapter reads off its stream — the
  limit that refused the run and, when the provider said, when it resets.
* ``quota_exhausted`` is the run's category. It is not a failure: the node goes
  back to ready, and the run row is ``refused``, which the epic overlay never
  latches as FAILED.
* :class:`RunnerQuotaHold` is the lane hold the run leaves in the store's meta
  table. The epic tick dispatches nothing to a held runner's lane until the
  reset passes, and ``kew doctor``'s ``runner-quota`` check reports it — both
  read the store, neither makes a paid call.

A hold has three ways out besides its reset: it never outlasts
:data:`MAX_HOLD` past the refusal, so a bogus ``resetsAt`` cannot park the
lane for a month; it stops applying once the runner's credential in the
environment is not the one the provider refused (a new token is a new
account window, so the next dispatch re-probes); and an operator can clear it
with ``kew quota clear <runner>``, which records who cleared it and when.

The same stream also warns before it refuses (#3329): a ``rate_limit_event``
with ``status=allowed_warning`` says the window is filling up.
:class:`QuotaWarning` is that notice, and :class:`RunnerQuotaWarning` is the
one per runner and limit in the meta table, under the credential fingerprint
it was seen with. It holds nothing: the epic surfaces name the token and the
limit while the warning still applies — until its reset passes, or until the
runner's latest run launched under a different credential.

Every hold or wait a usage limit causes ends with the token it refused
(#3330, :func:`refused_token`): a lane hold with the credential it stored, a
reviewer's wait with the :class:`RunCredential` its limited round ran under.

A contained agent can write that stream, so a warning is advisory and never
gates dispatch: an unknown limit name is "a usage limit", a reset outside
``[seen, seen + MAX_HOLD]`` is unknown, and a notice cannot pull a warning in
force back to an earlier reset.

Leaf module: stdlib only, so the stream fold, the runner adapters, dispatch,
the tick and doctor can all import it without a cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

#: The run category a quota rejection earns. Distinct from every failure kind.
QUOTA_EXHAUSTED_CATEGORY = "quota_exhausted"

#: Reason prefix on a frontier node the tick kept back for a held runner lane.
RUNNER_QUOTA_HELD_PREFIX = "runner-quota-held"

#: StreamEvent type and metadata key a runner adapter reports a rejection under.
QUOTA_EVENT_TYPE = "quota_exhausted"
QUOTA_REJECTION_KEY = "quota_rejection"

#: How long a lane stays held when the provider gave no reset time. Long enough
#: to stop a loop hammering the wall, short enough that a transient refusal
#: does not park the fleet for a day.
FALLBACK_HOLD = timedelta(hours=1)

#: The longest a hold lasts past the refusal, whatever reset the provider gave:
#: longer than any provider window (seven days), so a real reset is never cut
#: short, and short enough that a misread ``resetsAt`` cannot park the lane.
MAX_HOLD = timedelta(days=8)

#: Hex characters of SHA-256 kept as a credential fingerprint. Enough to tell
#: two tokens apart; far too few to say anything about the value itself.
CREDENTIAL_FINGERPRINT_CHARS = 12

_HOLD_META_PREFIX = "runner_quota_hold:"
_WARNING_META_PREFIX = "runner_quota_warning:"
_LAUNCH_META_PREFIX = "runner_credential_seen:"
_RUN_CREDENTIAL_META_PREFIX = "run_credential:"
_MAX_TOKEN_CHARS = 64

#: The provider windows a usage warning may name. The name comes off a stream a
#: contained agent can write to, and it reaches status, watch and the desktop
#: notification, so any other name is read as "a usage limit".
KNOWN_LIMIT_TYPES = frozenset(
    {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"}
)
#: The stored-warning key suffix for a warning whose limit kew does not know.
_UNNAMED_LIMIT = "unnamed"

#: Claude Code's ``rate_limit_event``: only ``rejected`` is the provider
#: refusing; ``allowed_warning`` is a window filling up, worth a warning;
#: ``allowed`` is routine.
_RATE_LIMIT_EVENT_TYPE = "rate_limit_event"
_REJECTED_STATUS = "rejected"
_WARNING_STATUS = "allowed_warning"

#: StreamEvent type and metadata key a runner adapter reports a warning under.
QUOTA_WARNING_EVENT_TYPE = "quota_warning"
QUOTA_WARNING_KEY = "quota_warning"

#: How long a warning applies when the provider gave no reset time.
FALLBACK_WARNING = timedelta(hours=5)

#: The CLI's out-of-credits error text, lower-cased. Matched only on a result
#: event that already says it is an error.
_OUT_OF_CREDITS_MARKERS = (
    "credit balance is too low",
    "out of credits",
    "out_of_credits",
)


def _token(value: object) -> str:
    """A short, safe identifier token (limit type, runner name), or ""."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_.")
    return cleaned[:_MAX_TOKEN_CHARS]


def _limit_type(value: object) -> str:
    """A usage warning's limit name if kew knows it, else "" ("a usage limit")."""
    name = _token(value)
    return name if name in KNOWN_LIMIT_TYPES else ""


def _reset_time(value: object) -> datetime | None:
    """A provider reset time: epoch seconds or ISO-8601, in UTC.

    None when unreadable, and when it has no UTC form: ``9999-12-31T23:59:59``
    at a negative offset parses, then overflows on conversion.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            _logger.warning("quota reset time out of range; reading it as unknown")
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(
                UTC
            )
        except (OverflowError, ValueError):
            _logger.warning("quota reset time unreadable; reading it as unknown")
            return None
    return None


def _fingerprint_token(value: object) -> str:
    """A stored fingerprint, kept only if it is the expected short hex."""
    if not isinstance(value, str) or len(value) != CREDENTIAL_FINGERPRINT_CHARS:
        return ""
    return value if all(ch in "0123456789abcdef" for ch in value) else ""


def credential_fingerprint(env: Mapping[str, str], names: Sequence[str]) -> str:
    """The first 12 hex of SHA-256 over the runner's credentials in *env*.

    *names* are the env vars the runner authenticates with, in the runner's
    declared order; every one that is set feeds the digest, so replacing any of
    them changes the fingerprint. "" when none is set — an unknown credential,
    which never releases a hold. The values themselves are never kept, logged
    or returned.
    """
    present = [(name, env[name]) for name in names if env.get(name)]
    if not present:
        return ""
    digest = hashlib.sha256()
    for name, value in present:
        digest.update(f"{name}\0{value}\0".encode())
    return digest.hexdigest()[:CREDENTIAL_FINGERPRINT_CHARS]


def agent_token(names: Sequence[str], fingerprint: str, runner: str = "") -> str:
    """``agent token NAME (fingerprint abc) for runner`` — never a value.

    The one wording every surface names a token in (#3329, #3330). No
    fingerprint leaves it out; no *runner* leaves ``for runner`` out.
    """
    joined = "/".join(names)
    subject = f"agent token {joined}" if joined else "agent token"
    if fingerprint:
        subject = f"{subject} (fingerprint {fingerprint})"
    return f"{subject} for {runner}" if runner else subject


def refused_token(names: Sequence[str], fingerprint: str) -> str:
    """The credential a provider limit refused, as a hold or wait ENDS with.

    ``agent token NAME (fingerprint abc)``, so the operator knows which token
    to raise or replace; ``unknown credential`` when kew never saw it. A
    reason longer than the operator surfaces' echo cap loses its middle and
    keeps its last 72 characters (:func:`kew.epic_holds.safe_hold_reason`):
    the names and the fingerprint fit in those, so a reason that ends with the
    token keeps it on every surface.
    """
    return agent_token(names, fingerprint) if fingerprint else "unknown credential"


#: The operator surfaces' echo cap on a hold or wait reason
#: (``kew.epic_holds.HOLD_REASON_MAX_CHARS``; a leaf cannot import it). A
#: reason within it reads the same on every surface.
REASON_MAX_CHARS = 160


def first_that_fits(*candidates: str) -> str:
    """The first of *candidates*, longest wording first, within the echo cap;
    the last when none is."""
    for candidate in candidates:
        if len(candidate) <= REASON_MAX_CHARS:
            return candidate
    return candidates[-1]


def _iso(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class QuotaRejection:
    """The provider refused a runner for quota. ``resets_at`` None = unknown."""

    runner: str = ""
    limit_type: str = ""
    resets_at: datetime | None = None

    def with_runner(self, runner: str) -> QuotaRejection:
        return QuotaRejection(_token(runner), self.limit_type, self.resets_at)

    def describe(self) -> str:
        """``claude-code seven_day quota exhausted, resets <iso>``."""
        subject = " ".join(p for p in (self.runner, self.limit_type) if p)
        head = f"{subject} quota exhausted" if subject else "quota exhausted"
        if self.resets_at is None:
            return f"{head}, reset time unknown"
        return f"{head}, resets {_iso(self.resets_at)}"

    def as_json(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "limit_type": self.limit_type,
            "resets_at": _iso(self.resets_at) if self.resets_at else None,
        }

    @classmethod
    def from_json(cls, raw: object) -> QuotaRejection | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            runner=_token(raw.get("runner")),
            limit_type=_token(raw.get("limit_type")),
            resets_at=_reset_time(raw.get("resets_at")),
        )


def rejection_from_rate_limit_event(event: Mapping[str, Any]) -> QuotaRejection | None:
    """A Claude Code ``rate_limit_event`` that REFUSED the run, else None."""
    if event.get("type") != _RATE_LIMIT_EVENT_TYPE:
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict) or info.get("status") != _REJECTED_STATUS:
        return None
    return QuotaRejection(
        limit_type=_token(info.get("rateLimitType")),
        resets_at=_reset_time(info.get("resetsAt")),
    )


def _fraction(value: object) -> float | None:
    """A utilization or threshold in [0, 1], else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


@dataclass(frozen=True)
class QuotaWarning:
    """The provider warned that a runner's usage window is filling up.

    ``utilization`` and ``threshold`` are fractions in [0, 1], None when the
    notice did not carry them; ``resets_at`` None = unknown.
    """

    runner: str = ""
    limit_type: str = ""
    resets_at: datetime | None = None
    utilization: float | None = None
    threshold: float | None = None

    def with_runner(self, runner: str) -> QuotaWarning:
        return replace(self, runner=_token(runner))

    def describe(self, *, usage: bool = True) -> str:
        """``near its seven_day limit (92% used), resets <iso>``.

        ``usage=False`` leaves the percentage out: it rises notice by notice
        within one window, so a line a watcher de-dups on must not carry it.
        """
        limit = f"its {self.limit_type} limit" if self.limit_type else "a usage limit"
        used = self.utilization if self.utilization is not None else self.threshold
        head = f"near {limit}"
        if usage and used is not None:
            head = f"{head} ({round(used * 100)}% used)"
        if self.resets_at is None:
            return f"{head}, reset time unknown"
        return f"{head}, resets {_iso(self.resets_at)}"

    def as_json(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "limit_type": self.limit_type,
            "resets_at": _iso(self.resets_at) if self.resets_at else None,
            "utilization": self.utilization,
            "threshold": self.threshold,
        }

    @classmethod
    def from_json(cls, raw: object) -> QuotaWarning | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            runner=_token(raw.get("runner")),
            limit_type=_limit_type(raw.get("limit_type")),
            resets_at=_reset_time(raw.get("resets_at")),
            utilization=_fraction(raw.get("utilization")),
            threshold=_fraction(raw.get("threshold")),
        )


def warning_from_rate_limit_event(event: Mapping[str, Any]) -> QuotaWarning | None:
    """A Claude Code ``rate_limit_event`` WARNING of a filling window, else None.

    Only ``allowed_warning``: ``allowed`` is routine traffic, and ``rejected``
    is the refusal :func:`rejection_from_rate_limit_event` reads.
    """
    if event.get("type") != _RATE_LIMIT_EVENT_TYPE:
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict) or info.get("status") != _WARNING_STATUS:
        return None
    return QuotaWarning(
        limit_type=_limit_type(info.get("rateLimitType")),
        resets_at=_reset_time(info.get("resetsAt")),
        utilization=_fraction(info.get("utilization")),
        threshold=_fraction(info.get("surpassedThreshold")),
    )


def rejection_from_result_event(event: Mapping[str, Any]) -> QuotaRejection | None:
    """An error result whose text is the CLI's out-of-credits error, else None."""
    if event.get("is_error") is not True:
        return None
    text = event.get("result")
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    if not any(marker in lowered for marker in _OUT_OF_CREDITS_MARKERS):
        return None
    return QuotaRejection(limit_type="credits")


#: Categories a quota rejection replaces: the generic verdicts a two-second,
#: zero-token refusal is read as. Budget, kill and watchdog verdicts are kew's
#: own and keep their meaning.
_REPLACEABLE_CATEGORIES = frozenset({"", "permanent", "transient"})


def is_quota_exhausted(
    *, category: str, exit_code: int, rejection: QuotaRejection | None
) -> bool:
    """Whether a finished run is ``quota_exhausted`` rather than its category.

    A rejection only explains a run that did not succeed: a run that exited
    0 got served after all (an overage, a retried request), and keeps its
    success.
    """
    return (
        rejection is not None and exit_code != 0 and category in _REPLACEABLE_CATEGORIES
    )


@dataclass(frozen=True)
class HoldClearance:
    """An operator released a hold early: who, and when."""

    cleared_by: str
    cleared_at: datetime

    def describe(self) -> str:
        return f"cleared by {self.cleared_by or 'unknown'} at {_iso(self.cleared_at)}"


@dataclass(frozen=True)
class RunnerQuotaHold:
    """A runner lane held after a quota rejection, until ``until``.

    ``credential_fingerprint`` names the credential the provider refused
    (:func:`credential_fingerprint`), "" when none was in the environment;
    ``credential_names`` are the env vars it was read from (names only, never
    values). ``cleared`` is set once an operator released the hold.
    """

    rejection: QuotaRejection
    observed_at: datetime
    run_id: str = ""
    credential_fingerprint: str = ""
    cleared: HoldClearance | None = None
    credential_names: tuple[str, ...] = ()

    @property
    def runner(self) -> str:
        return self.rejection.runner

    @property
    def until(self) -> datetime:
        """The provider's reset (or the fallback), never past :data:`MAX_HOLD`."""
        wanted = self.rejection.resets_at or self.observed_at + FALLBACK_HOLD
        return min(wanted, self.observed_at + MAX_HOLD)

    def active(self, now: datetime | None = None) -> bool:
        return self.cleared is None and (now or datetime.now(UTC)) < self.until

    def released_by_credential(self, current_fingerprint: str | None) -> bool:
        """Whether the environment now carries a different credential.

        Only two KNOWN fingerprints that differ release the hold: a hold
        recorded without one, or an environment without one, keeps holding —
        kew cannot tell that the account changed.
        """
        return bool(
            self.credential_fingerprint
            and current_fingerprint
            and current_fingerprint != self.credential_fingerprint
        )

    def applies(
        self, now: datetime | None = None, *, credential_fingerprint: str | None = None
    ) -> bool:
        """Active, and not released by a credential change."""
        return self.active(now) and not self.released_by_credential(
            credential_fingerprint
        )

    def token(self) -> str:
        """The refused credential (:func:`refused_token`) — never a value."""
        return refused_token(self.credential_names, self.credential_fingerprint)

    def reason(self) -> str:
        """The operator-facing hold reason, prefix first so it buckets.

        It ends with the refused token (#3330), so the operator knows which
        one to raise or replace. A token named by two env vars leaves no room
        for every time the hold knows within the echo cap, so the wording
        shortens to fit — the time the lane frees is the one always kept.
        """
        until = _iso(self.until)
        subject = " ".join(p for p in (self.runner, self.rejection.limit_type) if p)
        full = self.rejection.describe()
        if self.rejection.resets_at != self.until:
            full = f"{full}; runner lane held until {until}"
        tail = f"; {self.token()}"
        return first_that_fits(
            f"{RUNNER_QUOTA_HELD_PREFIX}: {full}{tail}",
            f"{RUNNER_QUOTA_HELD_PREFIX}: {subject} quota exhausted, held until "
            f"{until}{tail}",
            f"{RUNNER_QUOTA_HELD_PREFIX}: {subject} until {until}{tail}",
            # The limit name comes off a stream the agent can write: one long
            # enough to crowd out the token is what goes.
            f"{RUNNER_QUOTA_HELD_PREFIX}: {self.runner} until {until}{tail}",
        )

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rejection": self.rejection.as_json(),
            "observed_at": _iso(self.observed_at),
            "run_id": _token(self.run_id),
            "credential_fingerprint": _fingerprint_token(self.credential_fingerprint),
            "credential_names": list(_names(self.credential_names)),
        }
        if self.cleared is not None:
            out["cleared_by"] = _token(self.cleared.cleared_by)
            out["cleared_at"] = _iso(self.cleared.cleared_at)
        return out

    @classmethod
    def from_json(cls, raw: object) -> RunnerQuotaHold | None:
        if not isinstance(raw, dict):
            return None
        rejection = QuotaRejection.from_json(raw.get("rejection"))
        observed_at = _reset_time(raw.get("observed_at"))
        if rejection is None or not rejection.runner or observed_at is None:
            return None
        cleared_at = _reset_time(raw.get("cleared_at"))
        cleared = (
            HoldClearance(_token(raw.get("cleared_by")), cleared_at)
            if cleared_at is not None
            else None
        )
        return cls(
            rejection,
            observed_at,
            _token(raw.get("run_id")),
            _fingerprint_token(raw.get("credential_fingerprint")),
            cleared,
            _names(raw.get("credential_names")),
        )


def record_runner_quota_hold(
    store: RunStore,
    rejection: QuotaRejection,
    *,
    run_id: str = "",
    credential_fingerprint: str = "",
    credential_names: Sequence[str] = (),
    now: datetime | None = None,
) -> RunnerQuotaHold | None:
    """Hold *rejection*'s runner lane. Latest rejection wins. None if unnamed.

    *credential_fingerprint* is the refused credential's fingerprint (never
    the value), so a later dispatch under a different credential re-probes;
    *credential_names* are the env vars it was read from, so the hold names
    the token.
    """
    if not rejection.runner:
        return None
    hold = RunnerQuotaHold(
        rejection,
        now or datetime.now(UTC),
        run_id,
        _fingerprint_token(credential_fingerprint),
        credential_names=_names(tuple(credential_names)),
    )
    store.set_meta(_HOLD_META_PREFIX + rejection.runner, json.dumps(hold.as_json()))
    return hold


def read_runner_quota_hold(store: RunStore, runner: str) -> RunnerQuotaHold | None:
    """The last recorded hold for *runner*, active or not; None when absent."""
    raw = store.get_meta(_HOLD_META_PREFIX + _token(runner))
    if not raw:
        return None
    try:
        return RunnerQuotaHold.from_json(json.loads(raw))
    except ValueError:
        _logger.warning("runner quota hold for %s is unreadable; ignored", runner)
        return None


def active_runner_quota_hold(
    store: RunStore,
    runner: str,
    *,
    now: datetime | None = None,
    credential_fingerprint: str | None = None,
) -> RunnerQuotaHold | None:
    """*runner*'s hold while it still applies, else None.

    Pass the fingerprint of the credential the next dispatch would use: a hold
    recorded under a different one no longer applies.
    """
    hold = read_runner_quota_hold(store, runner)
    if hold is None or not hold.applies(
        now, credential_fingerprint=credential_fingerprint
    ):
        return None
    return hold


def clear_runner_quota_hold(
    store: RunStore,
    runner: str,
    *,
    cleared_by: str,
    now: datetime | None = None,
) -> RunnerQuotaHold | None:
    """Release *runner*'s active hold early; the hold as it stood, or None.

    The cleared hold stays in the store marked with who cleared it and when,
    so ``kew doctor`` and a later reader see the release, not a gap. None —
    and nothing written — when no hold is active.
    """
    moment = now or datetime.now(UTC)
    hold = read_runner_quota_hold(store, runner)
    if hold is None or not hold.active(moment):
        return None
    clearance = HoldClearance(_token(cleared_by), moment)
    released = RunnerQuotaHold(
        hold.rejection,
        hold.observed_at,
        hold.run_id,
        hold.credential_fingerprint,
        clearance,
        hold.credential_names,
    )
    store.set_meta(_HOLD_META_PREFIX + hold.runner, json.dumps(released.as_json()))
    _logger.warning(
        "runner quota hold on %s %s (was: %s)",
        hold.runner,
        clearance.describe(),
        hold.rejection.describe(),
    )
    return hold


def _names(value: object) -> tuple[str, ...]:
    """Stored credential env var names, each kept only as a safe token."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(name for item in value if (name := _token(item)))


@dataclass(frozen=True)
class RunnerQuotaWarning:
    """The latest usage warning a runner's stream carried, as stored.

    ``credential_names`` are the env vars the runner authenticates with (names
    only, never values); ``credential_fingerprint`` is the credential the
    warning was seen under (:func:`credential_fingerprint`).
    """

    warning: QuotaWarning
    observed_at: datetime
    run_id: str = ""
    credential_fingerprint: str = ""
    credential_names: tuple[str, ...] = ()

    @property
    def runner(self) -> str:
        return self.warning.runner

    @property
    def until(self) -> datetime:
        """The provider's reset (or the fallback), never past :data:`MAX_HOLD`."""
        wanted = self.warning.resets_at or self.observed_at + FALLBACK_WARNING
        return min(wanted, self.observed_at + MAX_HOLD)

    def applies(
        self, now: datetime | None = None, *, credential_fingerprint: str | None = None
    ) -> bool:
        """Before its reset, and the credential has not been replaced since.

        *credential_fingerprint* is the one the runner's latest run LAUNCHED
        under (:func:`read_runner_launch_credential`), never a reader's own
        environment. As for a hold, only two KNOWN fingerprints that differ say
        the token was replaced: a new token is a new window, so its warning is
        stale.
        """
        replaced = bool(
            self.credential_fingerprint
            and credential_fingerprint
            and credential_fingerprint != self.credential_fingerprint
        )
        return not replaced and (now or datetime.now(UTC)) < self.until

    def token(self) -> str:
        """``agent token NAME (fingerprint abc) for runner`` — never a value."""
        return agent_token(
            self.credential_names, self.credential_fingerprint, self.runner
        )

    def reason(self, *, usage: bool = True) -> str:
        """The operator line: which token, which limit, when it resets.

        ``usage=False`` is the attention event's reason: an event's
        ``state_key`` includes its reason, so the percentage — which rises
        with each repeated notice — would make every notice a new event.
        """
        return f"{self.token()} is {self.warning.describe(usage=usage)}"

    @property
    def item(self) -> str:
        """The attention item: the runner, and the limit when kew knows it.

        One runner can be near two windows at once; a watcher remembers one
        state per item, so each window needs its own.
        """
        limit = self.warning.limit_type
        return f"{self.runner}:{limit}" if limit else self.runner

    @property
    def identity(self) -> str:
        """What makes this a distinct warning state: token, limit and window.

        A repeated notice in the same window keeps the same identity, so a
        watcher reports it once; the next window, or a new token, is new.
        """
        resets = _iso(self.warning.resets_at) if self.warning.resets_at else ""
        return ":".join((self.credential_fingerprint, self.warning.limit_type, resets))

    def as_json(self) -> dict[str, Any]:
        return {
            "warning": self.warning.as_json(),
            "observed_at": _iso(self.observed_at),
            "run_id": _token(self.run_id),
            "credential_fingerprint": _fingerprint_token(self.credential_fingerprint),
            "credential_names": list(_names(self.credential_names)),
        }

    @classmethod
    def from_json(cls, raw: object) -> RunnerQuotaWarning | None:
        if not isinstance(raw, dict):
            return None
        warning = QuotaWarning.from_json(raw.get("warning"))
        observed_at = _reset_time(raw.get("observed_at"))
        if warning is None or not warning.runner or observed_at is None:
            return None
        return cls(
            warning,
            observed_at,
            _token(raw.get("run_id")),
            _fingerprint_token(raw.get("credential_fingerprint")),
            _names(raw.get("credential_names")),
        )


def record_runner_quota_warning(
    store: RunStore,
    warning: QuotaWarning,
    *,
    run_id: str = "",
    credential_fingerprint: str = "",
    credential_names: Sequence[str] = (),
    now: datetime | None = None,
) -> RunnerQuotaWarning | None:
    """Record *warning* for its runner and limit. None when nothing was written.

    *credential_fingerprint* is the fingerprint of the credential the warning
    was seen under and *credential_names* the env vars it is read from — never
    the value.

    The notice comes off a stream the agent can write to, so it is trusted
    only as far as it cannot hide or bend a real one: a limit kew does not know
    is "a usage limit", a reset outside ``[now, now + MAX_HOLD]`` is unknown,
    and a notice that would pull a warning still in force for the same
    credential back to an earlier reset, or to none, is not written.
    """
    if not warning.runner:
        return None
    moment = now or datetime.now(UTC)
    record = RunnerQuotaWarning(
        _within_window(
            replace(warning, limit_type=_limit_type(warning.limit_type)), moment
        ),
        moment,
        run_id,
        _fingerprint_token(credential_fingerprint),
        _names(tuple(credential_names)),
    )
    stored = read_runner_quota_warning(store, record.runner, record.warning.limit_type)
    if stored is not None and _stands_against(stored, record):
        return None
    store.set_meta(
        _warning_key(record.runner, record.warning.limit_type),
        json.dumps(record.as_json()),
    )
    return record


def _warning_key(runner: str, limit_type: str) -> str:
    return f"{_WARNING_META_PREFIX}{_token(runner)}:{limit_type or _UNNAMED_LIMIT}"


def _within_window(warning: QuotaWarning, observed_at: datetime) -> QuotaWarning:
    """*warning* with a reset before *observed_at*, or past MAX_HOLD, unknown."""
    resets = warning.resets_at
    if resets is None or observed_at <= resets <= observed_at + MAX_HOLD:
        return warning
    _logger.warning("usage warning reset outside its window; reading it as unknown")
    return replace(warning, resets_at=None)


def _stands_against(stored: RunnerQuotaWarning, new: RunnerQuotaWarning) -> bool:
    """Whether *stored* keeps its place: same credential, still in force, and
    *new* carries no reset or an earlier one. The later reset is kept."""
    kept = stored.warning.resets_at
    if stored.credential_fingerprint != new.credential_fingerprint or kept is None:
        return False
    if new.observed_at >= stored.until:
        return False
    return new.warning.resets_at is None or new.warning.resets_at < kept


def read_runner_quota_warning(
    store: RunStore, runner: str, limit_type: str
) -> RunnerQuotaWarning | None:
    """The last warning recorded for *runner*'s *limit_type*, applying or not.

    *limit_type* outside :data:`KNOWN_LIMIT_TYPES` reads the one record kept
    for every limit kew does not know. None when absent or unreadable.
    """
    raw = store.get_meta(_warning_key(runner, _limit_type(limit_type)))
    if not raw:
        return None
    try:
        return RunnerQuotaWarning.from_json(json.loads(raw))
    except ValueError:
        _logger.warning("runner quota warning for %s is unreadable; ignored", runner)
        return None


def record_runner_launch_credential(
    store: RunStore, runner: str, fingerprint: str
) -> None:
    """Record the fingerprint *runner*'s run is launching under (#3329).

    The latest launch is what says a token was replaced: a warning recorded
    under any other fingerprint no longer applies. An empty or malformed
    fingerprint — no credential set — records nothing, and an unchanged one
    is not rewritten.
    """
    value = _fingerprint_token(fingerprint)
    key = _LAUNCH_META_PREFIX + _token(runner)
    if value and _token(runner) and store.get_meta(key) != value:
        store.set_meta(key, value)


def read_runner_launch_credential(store: RunStore, runner: str) -> str:
    """The fingerprint *runner*'s latest run launched under; "" when unknown."""
    return _fingerprint_token(store.get_meta(_LAUNCH_META_PREFIX + _token(runner)))


@dataclass(frozen=True)
class RunCredential:
    """The credential one run ran under: its runner, the env vars it was read
    from and their fingerprint — names and a digest, never a value. "" for
    *fingerprint* when none was set."""

    runner: str
    names: tuple[str, ...] = ()
    fingerprint: str = ""

    def token(self) -> str:
        """:func:`refused_token` — what a limit that refused it names."""
        return refused_token(self.names, self.fingerprint)

    def replaced_by(self, current_fingerprint: str) -> bool:
        """Whether *current_fingerprint* is a different, known credential.

        As for a hold (:meth:`RunnerQuotaHold.released_by_credential`), only
        two KNOWN fingerprints that differ say the token was replaced.
        """
        return bool(
            self.fingerprint
            and current_fingerprint
            and current_fingerprint != self.fingerprint
        )


def record_run_credential(
    store: RunStore, run_id: str, credential: RunCredential
) -> None:
    """Record the credential run *run_id* ran under (#3330), so a limit it hit
    is named by the token it hit, whatever the environment holds later."""
    if not _token(run_id):
        return
    store.set_meta(
        _RUN_CREDENTIAL_META_PREFIX + _token(run_id),
        json.dumps(
            {
                "runner": _token(credential.runner),
                "names": list(_names(credential.names)),
                "fingerprint": _fingerprint_token(credential.fingerprint),
            }
        ),
    )


def read_run_credential(store: RunStore, run_id: str) -> RunCredential | None:
    """The credential :func:`record_run_credential` stored for *run_id*; None
    when none was recorded or the record is unreadable."""
    raw = store.get_meta(_RUN_CREDENTIAL_META_PREFIX + _token(run_id))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        _logger.warning("the limited-run record for %s is unreadable; ignored", run_id)
        return None
    runner = _token(parsed.get("runner")) if isinstance(parsed, dict) else ""
    if not isinstance(parsed, dict) or not runner:
        _logger.warning("the limited-run record for %s is unreadable; ignored", run_id)
        return None
    return RunCredential(
        runner,
        _names(parsed.get("names")),
        _fingerprint_token(parsed.get("fingerprint")),
    )


#: Every limit a warning is stored under, the unnamed one last.
_WARNING_LIMITS = (*sorted(KNOWN_LIMIT_TYPES), "")


def active_runner_quota_warnings(
    store: RunStore,
    runners: Iterable[str],
    *,
    now: datetime | None = None,
) -> tuple[RunnerQuotaWarning, ...]:
    """The warnings that still apply, in runner then limit order.

    A warning recorded under a credential other than the one the runner's
    latest run launched under was for a token since replaced, and is not
    returned. Whoever is reading — and whatever its own environment holds —
    gets the same answer.
    """
    found: list[RunnerQuotaWarning] = []
    for runner in sorted(set(runners)):
        launched = read_runner_launch_credential(store, runner)
        for limit in _WARNING_LIMITS:
            record = read_runner_quota_warning(store, runner, limit)
            if record is not None and record.applies(
                now, credential_fingerprint=launched
            ):
                found.append(record)
    return tuple(found)
