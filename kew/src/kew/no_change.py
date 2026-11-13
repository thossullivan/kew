"""I10A vocabulary: what a zero-diff exit means, and what it owes (Tasks 43-44).

A run that changed nothing is the one outcome kew cannot read off its own
artifacts. "The agent looked and there was genuinely nothing to do" and "the
agent never got as far as writing a file" produce the same empty diff, and the
legacy path treated the second as the first: it closed the item, commented on
it, and moved dependents forward on the strength of an absence.

So this module names one projection and nothing else:

``candidate`` → ``attested`` → ``pending_confirmation`` → ``authorized``
→ ``sync_pending`` → ``effect_pending`` → ``completed``

with ``rejected`` reachable from every live state, because refusing a
no-change is a decision somebody is allowed to make at any point. Movement is
monotonic: a judgment advances to a state of strictly higher rank, or to
``rejected``, or nowhere.

Three ideas carry the weight:

* **Attestation is the absence of named gaps.** :class:`NoChangeEvidence`
  answers *why not* rather than *yes/no* — a stale base, a work package that
  is not empty or does not cover the workspace, a check that did not run or
  did not pass, a change that was filtered, refused or reverted. Suppressed
  work is the sharpest case: an attempted change that kew removed is evidence
  that there WAS something to do, so it can never read as "nothing to do".
* **Policy authorizes; evidence never does.** The default is ``confirm`` — a
  person decides. ``never_accept`` is never satisfied by anything, not even a
  person. ``close_if_host_verified`` may authorize alone, and only against a
  trusted, versioned, per-item :class:`AlreadySatisfiedContract`.
* **Effect is generation-bound and one dependent at a time.** Sync jobs and
  dependency effects carry the judgment generation they were created from, so
  a replay under a newer generation cannot acknowledge an older intent, and
  dependents are ordered so a crash after the first cannot repeat it or expose
  the second early.

**Nothing here writes anything, anywhere.** Every function is pure over
values; :mod:`kew.run_store_no_change` owns the durable side, and I10B–I12 own
the effects. A legacy ``success``-with-no-PR row reads as
:data:`LEGACY_UNKNOWN` and is never backfilled: no legacy run recorded a
trusted base, a sealed package or a named check, so a candidate written on its
behalf would attest to evidence that never existed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kew.publication_artifact import WORKSPACE_SCOPE
from kew.sensitive_prompt import is_digest

_logger = logging.getLogger(__name__)

CANDIDATE = "candidate"
ATTESTED = "attested"
PENDING_CONFIRMATION = "pending_confirmation"
AUTHORIZED = "authorized"
SYNC_PENDING = "sync_pending"
EFFECT_PENDING = "effect_pending"
COMPLETED = "completed"
REJECTED = "rejected"

#: The projection in the order a judgment passes through it.
LIFECYCLE = (
    CANDIDATE,
    ATTESTED,
    PENDING_CONFIRMATION,
    AUTHORIZED,
    SYNC_PENDING,
    EFFECT_PENDING,
    COMPLETED,
)

#: States nothing may leave. ``completed`` is the only happy one.
TERMINAL_STATES = frozenset({COMPLETED, REJECTED})

_RANK = {state: index for index, state in enumerate(LIFECYCLE)}

CONFIRM = "confirm"
NEVER_ACCEPT = "never_accept"
CLOSE_IF_HOST_VERIFIED = "close_if_host_verified"

#: Every policy a no-change outcome may be resolved under.
NO_CHANGE_POLICIES = (CONFIRM, NEVER_ACCEPT, CLOSE_IF_HOST_VERIFIED)

DEFAULT_NO_CHANGE_POLICY = CONFIRM
"""Confirm. A no-change closes an item without producing anything to read, so
the safe default is that a person says so."""

ALREADY_SATISFIED_CONTRACT_VERSION = 1
"""The one contract version ``close_if_host_verified`` trusts. A row carrying
any other version replays as untrusted rather than as a guess about what the
producer meant."""

CHECK_PASSED = "passed"

SYNC_INTENT = "intent"
SYNC_OBSERVED = "observed"
SYNC_ACKED = "acked"
SYNC_ABANDONED = "abandoned"

#: The synchronization lifecycle: what the host means to write, what it saw
#: when it looked, and the durable acknowledgement that ends the job.
SYNC_LIFECYCLE = (SYNC_INTENT, SYNC_OBSERVED, SYNC_ACKED)
SYNC_TERMINAL_STATES = frozenset({SYNC_ACKED, SYNC_ABANDONED})
_SYNC_RANK = {state: index for index, state in enumerate(SYNC_LIFECYCLE)}

EFFECT_INTENT = "intent"
EFFECT_APPLYING = "applying"
EFFECT_ACKED = "acked"
EFFECT_ABANDONED = "abandoned"

#: The per-dependent effect lifecycle. ``applying`` is what a crash leaves
#: behind, and it is deliberately not re-enterable: a resumed host must
#: observe what happened rather than apply a second time.
EFFECT_LIFECYCLE = (EFFECT_INTENT, EFFECT_APPLYING, EFFECT_ACKED)
EFFECT_TERMINAL_STATES = frozenset({EFFECT_ACKED, EFFECT_ABANDONED})
_EFFECT_RANK = {state: index for index, state in enumerate(EFFECT_LIFECYCLE)}

LEGACY_UNKNOWN = "unknown"
"""What a pre-I10A zero-change row is worth: nothing anyone may act on."""

SYNC_IDEMPOTENCY_PREFIX = "kew-no-change:"


#: The attempted-change vocabulary, restated rather than imported.
#:
#: :mod:`kew.evidence` sits ABOVE the store in the layered dispatch stack
#: (it reaches the audit ledger, which reaches the config, which reaches the
#: runners), and this module is composed into the store — so importing it here
#: would invert the stack. The values are the ledger's, and
#: ``test_no_change.py`` fails the moment the two lists disagree.
ATTEMPTED_CHANGE_DISPOSITIONS = (
    "filtered",
    "reverted",
    "dropped",
    "refused",
    "preserved",
    "unavailable",
)

#: The strict-evidence payload contract this module writes to, mirroring
#: :mod:`kew.evidence` for the same reason and pinned by the same test.
EVIDENCE_SCHEMA_VERSION = 1
RECEIPT_FACT_EVENT = "evidence.receipt_fact"
EVIDENCE_PRODUCER_NAME = "kew.no_change"
EVIDENCE_ACTOR_TYPE = "policy"
EVIDENCE_ACTOR_ID = "no-change-judgment"

JUDGMENT_STATE_FACT = "no_change_judgment_state"
SYNC_STATE_FACT = "outcome_sync_state"
EFFECT_STATE_FACT = "no_change_dependency_effect_state"


def producer_version() -> str:
    """kew's version, read from installed metadata rather than ``kew``.

    ``kew/__init__`` pulls in the config, which pulls in the runner registry,
    and the store sits below both — so the package's own ``__version__`` is
    not importable from here without inverting the layered dispatch stack.
    ``test_no_change.py`` asserts the two agree.
    """
    try:
        return importlib.metadata.version("kew-cli")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - unpackaged
        return "unknown"


def build_receipt_fact_payload(
    *,
    fact: str,
    subject: str,
    target: str,
    detail: str,
    attempt_number: int,
    producer_version: str,
) -> dict[str, Any]:
    """The audit payload one no-change transition rests on.

    Byte-identical to what :func:`kew.evidence.record_evidence` would build for
    a host-produced ``evidence.receipt_fact``, so the ledger holds one evidence
    vocabulary rather than two. ``test_receipt_fact_payload_matches_kew_evidence``
    is what keeps that true.
    """
    return {
        "evidence_schema": EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": RECEIPT_FACT_EVENT,
        "producer_kind": "host",
        "producer_name": EVIDENCE_PRODUCER_NAME,
        "producer_version": producer_version,
        "attested_by_host": True,
        "facts": {
            "fact": fact,
            "value": target,
            "source": subject,
            "attempt_number": attempt_number,
            "detail": detail or f"{subject} -> {target}",
        },
    }


class NoChangeError(RuntimeError):
    """Base class for no-change refusals."""


class NoChangeValueError(NoChangeError):
    """A field was not something a no-change judgment can be built from."""


class NoChangeStateError(NoChangeError):
    """The projection does not allow the move that was asked for."""


class NoChangeConflictError(NoChangeError):
    """This lineage already has a live judgment, and two would race."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NoChangeValueError(message)


def _is_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def _key(*parts: object) -> str:
    """A stable digest over an ordered tuple of key parts.

    ``\\n`` joins rather than concatenation so ``("ab", "c")`` and
    ``("a", "bc")`` cannot collide into one key.
    """
    preimage = "\n".join(str(part) for part in parts)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NamedCheck:
    """One check the host ran, and the evidence row that proves it ran."""

    name: str
    outcome: str
    digest: str


@dataclass(frozen=True)
class AttemptedChangeRecord:
    """One change that was attempted, and what became of it.

    ``disposition`` is an :class:`kew.evidence.AttemptedChange` value, so a
    change kew filtered and a change the agent reverted stay distinguishable
    here exactly as they do in the ledger.
    """

    subject: str
    disposition: str
    reason: str = ""


@dataclass(frozen=True)
class NoChangeEvidence:
    """Everything the host knows about a zero-diff exit.

    Read through :meth:`gaps`: the answer to "may this be attested?" is the
    list of reasons it may not, so a refusal can be shown to a person instead
    of leaving them with a boolean.
    """

    trusted_base_sha: str
    observed_base_sha: str
    package_digest: str
    package_scope: str
    package_entry_count: int
    package_deletion_count: int
    checks: tuple[NamedCheck, ...] = ()
    attempted_changes: tuple[AttemptedChangeRecord, ...] = ()
    #: The repository paths the judgment is about (:func:`judged_paths_from`).
    #: A base that moved without touching any of them leaves the judgment
    #: standing; none recorded means every move stales it, as it always did.
    judged_paths: tuple[str, ...] = ()

    def gaps(self) -> tuple[str, ...]:
        """Every reason this evidence does not attest to a no-change."""
        return (
            *self._base_gaps(),
            *self._package_gaps(),
            *self._check_gaps(),
            *self._attempt_gaps(),
        )

    @property
    def attested(self) -> bool:
        return not self.gaps()

    def _base_gaps(self) -> tuple[str, ...]:
        if not _is_oid(self.trusted_base_sha):
            return ("no trusted base sha was recorded for the run",)
        if not _is_oid(self.observed_base_sha):
            return ("the base at judgment time was not observed",)
        if self.observed_base_sha != self.trusted_base_sha:
            return (
                f"the base moved from {self.trusted_base_sha} to"
                f" {self.observed_base_sha} while the run was working",
            )
        return ()

    def _package_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if not is_digest(self.package_digest):
            gaps.append("no complete work package was sealed for the run")
        if self.package_scope != WORKSPACE_SCOPE:
            gaps.append(
                f"the work package declares {self.package_scope!r} rather than"
                f" {WORKSPACE_SCOPE!r}, so it cannot be read as a no-change"
            )
        if self.package_entry_count or self.package_deletion_count:
            gaps.append(
                f"the work package is not empty: {self.package_entry_count}"
                f" entries, {self.package_deletion_count} deletions"
            )
        return tuple(gaps)

    def _check_gaps(self) -> tuple[str, ...]:
        if not self.checks:
            return ("no named check ran, so nothing looked for work to do",)
        gaps: list[str] = []
        for check in self.checks:
            if not check.name.strip():
                gaps.append("a check was recorded without a name")
            elif check.outcome != CHECK_PASSED:
                gaps.append(f"named check {check.name!r} {check.outcome}")
            if not is_digest(check.digest):
                gaps.append(f"named check {check.name!r} carries no evidence digest")
        return tuple(gaps)

    def _attempt_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        for attempt in self.attempted_changes:
            _require(
                attempt.disposition in ATTEMPTED_CHANGE_DISPOSITIONS,
                f"{attempt.disposition!r} is not an attempted-change disposition",
            )
            gaps.append(
                f"an attempted change to {attempt.subject} was"
                f" {attempt.disposition}, so there was work to do"
            )
        return tuple(gaps)

    @property
    def digest(self) -> str:
        """A stable ``sha256:`` reference to exactly this evidence."""
        return "sha256:" + _key(self.to_json())

    def to_json(self) -> str:
        """Canonical JSON — sorted keys, no spacing drift, one digest."""
        return json.dumps(self._as_mapping(), sort_keys=True, separators=(",", ":"))

    def _as_mapping(self) -> dict[str, Any]:
        mapping = self._attested_mapping()
        # Only when present, so evidence recorded before the field existed
        # serializes, and digests, exactly as it did.
        if self.judged_paths:
            mapping["judged_paths"] = list(self.judged_paths)
        return mapping

    def _attested_mapping(self) -> dict[str, Any]:
        return {
            "trusted_base_sha": self.trusted_base_sha,
            "observed_base_sha": self.observed_base_sha,
            "package_digest": self.package_digest,
            "package_scope": self.package_scope,
            "package_entry_count": self.package_entry_count,
            "package_deletion_count": self.package_deletion_count,
            "checks": [
                {"name": c.name, "outcome": c.outcome, "digest": c.digest}
                for c in self.checks
            ],
            "attempted_changes": [
                {"subject": a.subject, "disposition": a.disposition, "reason": a.reason}
                for a in self.attempted_changes
            ],
        }

    @classmethod
    def from_json(cls, payload: str) -> NoChangeEvidence:
        """Rebuild evidence from a persisted row, refusing anything else."""
        try:
            document = json.loads(payload)
        except ValueError as exc:
            raise NoChangeValueError(f"stored evidence is not JSON: {exc}") from exc
        _require(isinstance(document, dict), "stored evidence is not an object")
        return cls._from_mapping(document)

    @classmethod
    def _from_mapping(cls, document: Mapping[str, Any]) -> NoChangeEvidence:
        return cls(
            trusted_base_sha=str(document.get("trusted_base_sha", "")),
            observed_base_sha=str(document.get("observed_base_sha", "")),
            package_digest=str(document.get("package_digest", "")),
            package_scope=str(document.get("package_scope", "")),
            package_entry_count=int(document.get("package_entry_count", 0)),
            package_deletion_count=int(document.get("package_deletion_count", 0)),
            checks=tuple(
                NamedCheck(
                    name=str(raw.get("name", "")),
                    outcome=str(raw.get("outcome", "")),
                    digest=str(raw.get("digest", "")),
                )
                for raw in document.get("checks", ())
            ),
            attempted_changes=tuple(
                AttemptedChangeRecord(
                    subject=str(raw.get("subject", "")),
                    disposition=str(raw.get("disposition", "")),
                    reason=str(raw.get("reason", "")),
                )
                for raw in document.get("attempted_changes", ())
            ),
            judged_paths=tuple(str(p) for p in document.get("judged_paths", ())),
        )


#: A run of characters a repository path can be spelled with in prose. ``:``
#: ends it, so ``src/app.py:12-30`` yields ``src/app.py``; ``*`` and ``?`` stay
#: in it, so a glob is seen as one and refused rather than split into words;
#: ``+`` and ``@`` stay in it, so ``+page.svelte`` is never read as
#: ``page.svelte``. Brackets, parentheses, quotes and commas end it.
_PATH_TOKEN = re.compile(r"[\w.*?~+@-][\w.*?~+@/-]*")
#: A URL names a page, not a path in this repository; it is dropped first.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
#: The last dot-segment of a name that reads as a file (``waits.py``,
#: ``kew.toml``) or as a dotted code reference (``kew.no_change``).
_SUFFIX = re.compile(r"[A-Za-z_]\w*")
#: Dotted prose that is neither a file nor code.
_ABBREVIATIONS = frozenset({"e.g", "i.e", "a.k.a", "n.b"})
#: What may wrap a name in prose without being part of it.
_WRAPPING = "\"'`()[]{}<>,;"
#: More than this many named paths is not a scope; record none instead.
_MAX_JUDGED_PATHS = 64


def _base_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _path_shaped(name: str, base_names: frozenset[str]) -> bool:
    """Whether *name* reads as a path, a file, or a dotted code reference.

    Deliberately broad: a name that only LOOKS like a path and resolves to no
    file costs the narrowing, never correctness (:func:`judged_paths_from`),
    while a real path read as prose would narrow the judgment to less than the
    item is about. A word that is the base name of a tracked file
    (``Makefile``, ``Dockerfile``, ``LICENSE``) counts, extension or not.
    """
    if "/" in name or name in base_names:
        return True
    stem, dot, suffix = name.rpartition(".")
    if not dot or name.lower() in _ABBREVIATIONS:
        return False
    return bool(_SUFFIX.fullmatch(suffix)) and (bool(stem) or name.startswith("."))


def _named_paths(text: str, base_names: frozenset[str]) -> tuple[str, ...]:
    """Every path-shaped name in *text*, in order, without duplicates."""
    names: list[str] = []
    for token in _PATH_TOKEN.findall(_URL.sub(" ", text)):
        name = token.strip(_WRAPPING).rstrip(".?")
        while name.startswith("./"):
            name = name[2:]
        if name and _path_shaped(name, base_names):
            names.append(name)
    return tuple(dict.fromkeys(names))


def _resolve_named_path(
    name: str,
    tracked: frozenset[str],
    files: Sequence[str],
    by_base_name: Mapping[str, Sequence[str]],
) -> str | None:
    """The one tracked file *name* means, or None when it does not mean one.

    A bare name (``waits.py``, ``README.md``) resolves when exactly one tracked
    file anywhere has that base name, the root included, so a root file never
    wins over its namesake in a subdirectory. A name with a directory resolves
    to itself, or else when exactly one tracked file ends in it at a directory
    boundary (``graph/driver/tick.py``). A glob, a directory, an untracked or
    ignored file, and a name two tracked files share all resolve to nothing.
    """
    if "*" in name or "?" in name or name.endswith("/"):
        return None
    if "/" not in name:
        matches = list(by_base_name.get(name, ()))
    elif name in tracked:
        return name
    else:
        matches = [path for path in files if path.endswith("/" + name)]
    return matches[0] if len(matches) == 1 else None


def judged_paths_from(
    text: str, tracked_files: Sequence[str] | None
) -> tuple[str, ...]:
    """The tracked files the work item's *text* names, or none at all.

    A no-change answers the question the item asked, and the item asks it about
    the files it names. The narrowing is all-or-nothing and fails safe: paths
    are recorded only when EVERY path-shaped name in the item resolves to
    exactly one file in *tracked_files* (repository-root-relative, as ``git
    ls-files`` lists them). A name that does not resolve, is ambiguous, names a
    directory or a glob — or an item that names no path, or more than a scope
    can mean — records none, and the judgment stays bound to its exact base.
    A judgment narrowed to the one docs page an item happened to name would
    otherwise stay confirmable after main rewrote the code it is about.
    ``None`` (the checkout's files could not be listed) records none too.
    """
    if tracked_files is None:
        return ()
    by_base_name: dict[str, list[str]] = {}
    for tracked_path in tracked_files:
        by_base_name.setdefault(_base_name(tracked_path), []).append(tracked_path)
    names = _named_paths(text, frozenset(by_base_name))
    if not names:
        return ()
    tracked = frozenset(tracked_files)
    resolved: list[str] = []
    for name in names:
        path = _resolve_named_path(name, tracked, tracked_files, by_base_name)
        if path is None:
            _logger.info("%r names no single tracked file; no judged paths", name)
            return ()
        resolved.append(path)
    unique = tuple(dict.fromkeys(resolved))
    return unique if len(unique) <= _MAX_JUDGED_PATHS else ()


@dataclass(frozen=True)
class NoChangeIntent:
    """The zero-diff exit the host means to record, before it records it."""

    source_kind: str
    source_id: str
    run_id: str
    attempt_number: int
    base_sha: str
    item_id: str | None = None

    @property
    def lineage_key(self) -> str:
        """One item, one no-change lineage — across runs and attempts."""
        return f"{self.source_kind}:{self.source_id}"

    @property
    def judgment_id(self) -> str:
        """Derived, so a creation retried after a crash converges on one row."""
        return (
            "nc-"
            + _key(self.source_kind, self.source_id, self.run_id, self.attempt_number)[
                :16
            ]
        )

    def validate(self) -> None:
        """Refuse an unbuildable intent BEFORE anything is written."""
        _require(bool(self.source_kind), "a no-change judgment needs a source kind")
        _require(bool(self.source_id), "a no-change judgment needs a source id")
        _require(bool(self.run_id), "a no-change judgment needs a run id")
        _require(
            isinstance(self.attempt_number, int) and self.attempt_number >= 1,
            f"attempt {self.attempt_number!r} is not an attempt number",
        )
        _require(_is_oid(self.base_sha), f"base {self.base_sha!r} is not a commit id")


@dataclass(frozen=True)
class AlreadySatisfiedContract:
    """A trusted, versioned, per-item statement that the outcome already holds.

    The ONLY thing ``close_if_host_verified`` may authorize against. Every
    field is a way of refusing something: an unknown version, another item's
    contract, a negative finding read as a positive one, and — the one that
    matters most — a contract the runner produced. A runner that could write
    its own already-satisfied contract could close any item by asserting it.
    """

    contract_version: int
    item_id: str
    satisfied: bool
    producer_kind: str
    verifier: str
    digest: str

    def validate(self, *, item_id: str) -> None:
        """Refuse a contract this host may not authorize a closure from."""
        _require(
            self.contract_version == ALREADY_SATISFIED_CONTRACT_VERSION,
            f"already-satisfied contract v{self.contract_version} is not the"
            f" trusted version v{ALREADY_SATISFIED_CONTRACT_VERSION}",
        )
        _require(
            self.item_id == item_id,
            f"the contract names item {self.item_id!r}, not {item_id!r}",
        )
        _require(self.satisfied is True, "the contract does not say it is satisfied")
        _require(
            self.producer_kind == "host",
            f"a {self.producer_kind!r}-produced contract is not host-verified",
        )
        _require(bool(self.verifier.strip()), "the contract names no verifier")
        _require(is_digest(self.digest), "the contract carries no evidence digest")


@dataclass(frozen=True)
class AuthorizationDecision:
    """What the policy alone resolves, and whether a person may go further.

    ``human_confirmable`` is not "was this authorized" — it is "is a human
    confirmation admissible at all". It is False under ``never_accept``,
    which is what makes that policy unsatisfiable rather than merely strict,
    and False on gapped evidence, because a person confirming an absence the
    host could not attest to is the exact failure this node exists to stop.
    """

    state: str
    reason: str
    human_confirmable: bool


def resolve_authorization(
    *,
    policy: str,
    evidence: NoChangeEvidence,
    item_id: str | None,
    contract: AlreadySatisfiedContract | None = None,
) -> AuthorizationDecision:
    """What *policy* authorizes for this evidence, with no person involved."""
    _require(policy in NO_CHANGE_POLICIES, f"{policy!r} is not a no-change policy")
    gaps = evidence.gaps()
    if gaps:
        return AuthorizationDecision(
            PENDING_CONFIRMATION,
            "the host could not attest to a no-change: " + "; ".join(gaps),
            human_confirmable=False,
        )
    if policy == NEVER_ACCEPT:
        return AuthorizationDecision(
            PENDING_CONFIRMATION,
            "never_accept is never satisfied: this item's no-change outcome"
            " cannot be authorized by evidence or by a person",
            human_confirmable=False,
        )
    if policy == CLOSE_IF_HOST_VERIFIED:
        return _resolve_host_verified(contract, item_id)
    return AuthorizationDecision(
        PENDING_CONFIRMATION,
        "the confirm policy requires a person to confirm the no-change outcome",
        human_confirmable=True,
    )


def _resolve_host_verified(
    contract: AlreadySatisfiedContract | None, item_id: str | None
) -> AuthorizationDecision:
    if contract is None:
        return AuthorizationDecision(
            PENDING_CONFIRMATION,
            "close_if_host_verified authorizes only against an already-satisfied"
            " contract, and none was offered",
            human_confirmable=True,
        )
    try:
        contract.validate(item_id=item_id or "")
    except NoChangeValueError as exc:
        return AuthorizationDecision(
            PENDING_CONFIRMATION,
            f"the already-satisfied contract is not trusted: {exc}",
            human_confirmable=True,
        )
    return AuthorizationDecision(
        AUTHORIZED,
        f"host-verified already-satisfied contract"
        f" v{contract.contract_version} for {contract.item_id}"
        f" from {contract.verifier}",
        human_confirmable=True,
    )


def _validate_monotonic(
    *,
    current: str,
    target: str,
    ranks: Mapping[str, int],
    terminal: frozenset[str],
    exits: frozenset[str],
    what: str,
    stepwise: bool = False,
) -> None:
    """Advance-only movement, optionally without skipping an intermediate state.

    The judgment may skip ``pending_confirmation`` — a policy that authorizes
    on its own never puts a person in the loop. The sync and effect lifecycles
    may not skip anything: their intermediate state is the observation, and a
    row that reached ``acked`` without one is an acknowledgement of a write
    nobody watched land.
    """
    if current in terminal:
        raise NoChangeStateError(
            f"a {current} {what} is settled and cannot become {target}"
        )
    if target in exits:
        return
    unknown = next((s for s in (current, target) if s not in ranks), None)
    if unknown is not None:
        raise NoChangeStateError(f"{unknown!r} is not a {what} state")
    lower, upper = ranks[current], ranks[target]
    allowed = (upper == lower + 1) if stepwise else (upper > lower)
    if not allowed:
        step = " one step at a time" if stepwise else ""
        raise NoChangeStateError(
            f"a {what} only advances{step}: {current} -> {target} is not a move"
        )


def validate_transition(*, current: str, target: str) -> None:
    """Refuse a judgment move the projection does not allow."""
    _validate_monotonic(
        current=current,
        target=target,
        ranks=_RANK,
        terminal=TERMINAL_STATES,
        exits=frozenset({REJECTED}),
        what="no-change judgment",
    )


def validate_outcome_sync_transition(*, current: str, target: str) -> None:
    """Refuse a sync-job move: intent, then observation, then acknowledgement."""
    _validate_monotonic(
        current=current,
        target=target,
        ranks=_SYNC_RANK,
        terminal=SYNC_TERMINAL_STATES,
        exits=frozenset({SYNC_ABANDONED}),
        what="outcome sync job",
        stepwise=True,
    )


def validate_dependency_effect_transition(*, current: str, target: str) -> None:
    """Refuse a dependency-effect move: intent, then applying, then ack."""
    _validate_monotonic(
        current=current,
        target=target,
        ranks=_EFFECT_RANK,
        terminal=EFFECT_TERMINAL_STATES,
        exits=frozenset({EFFECT_ABANDONED}),
        what="dependency effect",
        stepwise=True,
    )


def outcome_sync_job_id(
    judgment_id: str, generation: int, target_kind: str, target_ref: str
) -> str:
    """The one row a (judgment, generation, target) may ever have."""
    return "ncs-" + _key(judgment_id, generation, target_kind, target_ref)[:16]


def sync_idempotency_key(
    judgment_id: str, generation: int, target_kind: str, target_ref: str
) -> str:
    """The marker a remote write carries so a replay recognizes its own work.

    Generation-bound on purpose: a judgment that was superseded and re-decided
    must not be able to acknowledge itself with the previous generation's
    remote write.
    """
    return SYNC_IDEMPOTENCY_PREFIX + _key(
        judgment_id, generation, target_kind, target_ref
    )


def dependency_effect_id(judgment_id: str, generation: int, dependent_id: str) -> str:
    """The one row a (judgment, generation, dependent) may ever have."""
    return "nce-" + _key(judgment_id, generation, dependent_id)[:16]


@dataclass(frozen=True)
class LegacyNoChangeView:
    """A pre-I10A zero-change row, read as the nothing it proves.

    ``persisted`` is always False and exists to say so out loud: decoding a
    legacy row must never write a judgment on its behalf.
    """

    run_id: str
    attempt_number: int
    state: str
    reason: str
    item_id: str | None = None
    persisted: bool = False


def decode_legacy_no_change(
    *,
    status: str,
    run_id: str,
    attempt_number: int,
    pr_url: str | None = None,
    item_id: str | None = None,
) -> LegacyNoChangeView | None:
    """Read one legacy run row as ``unknown``, or as not a no-change at all.

    A successful run that opened no pull request is the shape a legacy
    zero-change exit left behind — and it is also the shape of a run that
    published by some other route, or crashed after success. None of those
    recorded a trusted base, a sealed package or a named check, so the honest
    answer is ``unknown``: not a candidate, and nothing anyone may close on.
    """
    if status != "success" or pr_url:
        return None
    return LegacyNoChangeView(
        run_id=run_id,
        attempt_number=attempt_number,
        item_id=item_id,
        state=LEGACY_UNKNOWN,
        reason=(
            "legacy successful run with no pull request: no trusted base, sealed"
            " work package or named check was recorded, so whether there was"
            " work to do is unknown"
        ),
    )


@dataclass(frozen=True)
class NoChangeJudgment:
    """One ``no_change_judgment`` row: the whole projection of one zero-diff."""

    judgment_id: str
    lineage_key: str
    generation: int
    source_kind: str
    source_id: str
    item_id: str | None
    run_id: str
    attempt_number: int
    base_sha: str
    state: str
    policy: str
    evidence_digest: str | None
    evidence_json: str | None
    decision_reason: str
    decided_by: str | None
    decided_at: str | None
    contract_digest: str | None
    audit_event_id: int | None
    created_at: str
    updated_at: str

    @property
    def live(self) -> bool:
        """True while this judgment still owns the lineage's outcome."""
        return self.state not in TERMINAL_STATES

    @property
    def evidence(self) -> NoChangeEvidence | None:
        """The attested evidence, or None while the judgment is a candidate."""
        if self.evidence_json is None:
            return None
        return NoChangeEvidence.from_json(self.evidence_json)


@dataclass(frozen=True)
class OutcomeSyncJob:
    """One ``outcome_sync_jobs`` row: one authorized write to one target."""

    sync_job_id: str
    judgment_id: str
    generation: int
    target_kind: str
    target_ref: str
    idempotency_key: str
    state: str
    observation_kind: str | None
    observation_ref: str | None
    observed_at: str | None
    acked_at: str | None
    detail: str
    audit_event_id: int | None
    created_at: str
    updated_at: str

    @property
    def settled(self) -> bool:
        return self.state in SYNC_TERMINAL_STATES


@dataclass(frozen=True)
class DependencyEffect:
    """One ``no_change_dependency_effect`` row: one dependent, one intent."""

    effect_id: str
    judgment_id: str
    generation: int
    dependent_id: str
    ordinal: int
    state: str
    applied_at: str | None
    acked_at: str | None
    detail: str
    audit_event_id: int | None
    created_at: str
    updated_at: str

    @property
    def settled(self) -> bool:
        return self.state in EFFECT_TERMINAL_STATES


# ---------------------------------------------------------------------------
# I11: who may decide a no-change outcome, and what a synchronization owes.
#
# Everything below is still pure. The authority rules are values in / decision
# out so the split that matters — an allowlist that a repository admin cannot
# talk past, and an empty allowlist that demands fresh repository evidence —
# is testable without a network, a store or a person.
# ---------------------------------------------------------------------------

REPOSITORY_WRITE = "write"
REPOSITORY_MAINTAIN = "maintain"
REPOSITORY_ADMIN = "admin"

#: The delivery-repository permissions that stand in for a named approver.
#: ``read`` and ``triage`` are deliberately absent: neither can land a commit,
#: so neither may decide that a commit was unnecessary.
AUTHORITATIVE_REPOSITORY_PERMISSIONS = (
    REPOSITORY_WRITE,
    REPOSITORY_MAINTAIN,
    REPOSITORY_ADMIN,
)

AUTHORITY_MAX_AGE_S = 900
"""How old repository-permission evidence may be and still be *fresh*.

Permission is revocable, and a no-change closure is irreversible from the
item's point of view. Fifteen minutes is long enough to cover one operator
session and short enough that a revoked collaborator cannot keep deciding."""

AUTHORITY_RULE_ALLOWLIST = "human_review_allowlist"
AUTHORITY_RULE_REPOSITORY = "delivery_repository_permission"
AUTHORITY_RULE_NONE = "unauthenticated"

#: How far ahead of the host's clock an observation may be stamped and still be
#: read as fresh rather than as evidence from a machine that disagrees.
_CLOCK_SKEW_S = 60

_AUTHORSHIP_NOTE = (
    "authorship of the item is not authority and neither grants nor removes it"
)


class NoChangeAuthorityError(NoChangeError):
    """The actor may not decide this item's no-change outcome."""


@dataclass(frozen=True)
class RepositoryAuthority:
    """What the delivery repository said about one actor, and when it said so.

    ``available`` False is the outage answer and is never read as "no
    permission is fine": an unread permission is refused exactly like a
    ``read`` one, because "we could not ask" and "the answer was no" have the
    same safe consequence here.
    """

    delivery_repo: str
    actor: str
    permission: str
    observed_at: str
    available: bool = True
    detail: str = ""

    @property
    def digest(self) -> str:
        """A stable ``sha256:`` reference to exactly this observation."""
        return "sha256:" + _key(
            self.delivery_repo,
            self.actor,
            self.permission,
            self.observed_at,
            self.available,
        )


@dataclass(frozen=True)
class AuthorityGrant:
    """One authority decision, with the evidence and the moment it was made."""

    authorized: bool
    rule: str
    actor: str
    reason: str
    evidence_digest: str
    decided_at: str

    @property
    def facts(self) -> dict[str, Any]:
        """The decision as an audit payload fragment."""
        return {
            "authorized": self.authorized,
            "authority_rule": self.rule,
            "authority_actor": self.actor,
            "authority_reason": self.reason,
            "authority_evidence": self.evidence_digest,
            "authority_decided_at": self.decided_at,
        }

    def require(self) -> AuthorityGrant:
        """Return the grant, or raise with the reason it was refused."""
        if not self.authorized:
            raise NoChangeAuthorityError(self.reason)
        return self


def _grant(
    *, authorized: bool, rule: str, actor: str, reason: str, digest: str, at: str
) -> AuthorityGrant:
    return AuthorityGrant(
        authorized=authorized,
        rule=rule,
        actor=actor,
        reason=reason,
        evidence_digest=digest,
        decided_at=at,
    )


def _authorship_suffix(actor: str, item_author: str | None) -> str:
    """The note an authorship claim earns: a sentence, never a permission."""
    if item_author and item_author == actor:
        return f" ({_AUTHORSHIP_NOTE})"
    return ""


def _allowlist_authority(
    *, actor: str, approvers: tuple[str, ...], delivery_repo: str, note: str, at: str
) -> AuthorityGrant:
    """Membership, and nothing else — not repository admin, not authorship."""
    member = actor in approvers
    reason = (
        f"{actor} is named in the human_review allowlist for {delivery_repo}"
        if member
        else (
            f"actor {actor!r} is not in the human_review allowlist for"
            f" {delivery_repo}; repository permission does not substitute for"
            " a named allowlist"
        )
    )
    return _grant(
        authorized=member,
        rule=AUTHORITY_RULE_ALLOWLIST,
        actor=actor,
        reason=reason + note,
        digest="sha256:" + _key(delivery_repo, actor, *approvers),
        at=at,
    )


def _authority_evidence_gap(
    *,
    actor: str,
    delivery_repo: str,
    authority: RepositoryAuthority | None,
    now: datetime,
    max_age_s: int,
) -> str:
    """Why this permission evidence does not authorize, or ``""``."""
    if authority is None or not authority.available:
        detail = f": {authority.detail}" if authority and authority.detail else ""
        return (
            f"the delivery repository's permission for {actor} could not be"
            f" read, and an unread permission is not write access{detail}"
        )
    if authority.actor != actor or authority.delivery_repo != delivery_repo:
        return (
            f"the permission evidence describes {authority.actor!r} on"
            f" {authority.delivery_repo!r}, not {actor!r} on {delivery_repo!r}"
        )
    if authority.permission not in AUTHORITATIVE_REPOSITORY_PERMISSIONS:
        return (
            f"{actor} has {authority.permission!r} on {delivery_repo}, which is"
            " not write, maintain or admin"
        )
    return _authority_freshness_gap(
        actor=actor, authority=authority, now=now, max_age_s=max_age_s
    )


def _authority_freshness_gap(
    *, actor: str, authority: RepositoryAuthority, now: datetime, max_age_s: int
) -> str:
    """Why this observation is not *fresh*, or ``""``. Permission is revocable."""
    age = _evidence_age_s(authority.observed_at, now)
    if age is None:
        return (
            f"the permission evidence for {actor} carries no readable"
            f" observation time ({authority.observed_at!r})"
        )
    if age > max_age_s or age < -_CLOCK_SKEW_S:
        return (
            f"the permission evidence for {actor} was observed at"
            f" {authority.observed_at}, which is not fresh within {max_age_s}s"
        )
    return ""


def _evidence_age_s(observed_at: str, now: datetime) -> float | None:
    """Seconds since *observed_at*, or None when it is not a readable time."""
    try:
        observed = datetime.fromisoformat(observed_at)
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return (now - observed).total_seconds()


def resolve_no_change_authority(
    *,
    actor: str,
    approvers: Sequence[str],
    delivery_repo: str,
    authority: RepositoryAuthority | None = None,
    item_author: str | None = None,
    now: datetime,
    max_age_s: int = AUTHORITY_MAX_AGE_S,
) -> AuthorityGrant:
    """Decide whether *actor* may confirm, reject or retry this outcome.

    Two rules, and exactly one of them is ever in force:

    * A **nonempty** ``[human_review].approvers`` allowlist requires
      membership. A repository admin who is not named is refused — the point
      of naming people is that the repository's own permissions stop deciding.
    * An **empty** allowlist requires fresh delivery-repository ``write``,
      ``maintain`` or ``admin`` evidence. This is stricter than the spend
      gate's "any authenticated actor", because closing an item on an absence
      leaves nothing for a reviewer to read afterwards.

    Authorship is not a third rule. An author who satisfies the rule in force
    is authorized like anyone else, and one who does not is refused like
    anyone else; the only trace authorship leaves is a note in the reason.
    """
    at = now.isoformat()
    note = _authorship_suffix(actor, item_author)
    if not actor or not actor.strip():
        return _grant(
            authorized=False,
            rule=AUTHORITY_RULE_NONE,
            actor=actor,
            reason="an unauthenticated actor cannot decide a no-change outcome" + note,
            digest="",
            at=at,
        )
    named = tuple(dict.fromkeys(a for a in approvers if a and a.strip()))
    if named:
        return _allowlist_authority(
            actor=actor,
            approvers=named,
            delivery_repo=delivery_repo,
            note=note,
            at=at,
        )
    gap = _authority_evidence_gap(
        actor=actor,
        delivery_repo=delivery_repo,
        authority=authority,
        now=now,
        max_age_s=max_age_s,
    )
    return _grant(
        authorized=not gap,
        rule=AUTHORITY_RULE_REPOSITORY,
        actor=actor,
        reason=(
            gap
            or f"{actor} holds {authority.permission!r} on {delivery_repo},"  # type: ignore[union-attr]
            f" observed at {authority.observed_at}"  # type: ignore[union-attr]
        )
        + note,
        digest=authority.digest if authority is not None else "",
        at=at,
    )


# ---- what the tracker shows, and what to do about it -----------------------

ITEM_OPEN = "open"
ITEM_CLOSED = "closed"
ITEM_UNKNOWN = "unknown"

SYNC_ACTION_ACK = "ack"
SYNC_ACTION_MUTATE = "mutate"
SYNC_ACTION_HOLD = "hold"

OBSERVED_ALREADY_SYNCHRONIZED = "already_synchronized"
OBSERVED_APPLIED = "applied"
OBSERVED_CONFLICT = "conflicting_remote_state"
OBSERVED_UNAVAILABLE = "tracker_unavailable"
OBSERVED_STALE_SOURCE = "stale_source"
OBSERVED_WRITE_UNCONFIRMED = "write_unconfirmed"


@dataclass(frozen=True)
class ItemObservation:
    """What one look at the tracker showed about the item being synchronized."""

    available: bool
    state: str = ITEM_UNKNOWN
    marker_found: bool = False
    ref: str | None = None
    detail: str = ""


def item_observation(
    *, closed: bool, bodies: Sequence[str], marker: str, ref: str, detail: str
) -> ItemObservation:
    """Build one available observation from what a tracker returned.

    Shared by both dialects on purpose: "does this item already carry this
    judgment's marker?" is the question idempotency rests on, and two
    implementations of it would be two chances to answer it differently.
    """
    return ItemObservation(
        available=True,
        state=ITEM_CLOSED if closed else ITEM_OPEN,
        marker_found=any(marker in body for body in bodies),
        ref=ref or None,
        detail=detail,
    )


@dataclass(frozen=True)
class SourceRevalidation:
    """Whether the base the judgment was made against is still the base."""

    valid: bool
    expected_base_sha: str
    observed_base_sha: str
    detail: str


@dataclass(frozen=True)
class SyncPlan:
    """One decision about one sync job: acknowledge, write, or hold.

    ``needs_comment`` is what makes the write resumable rather than merely
    repeatable. A pass that commented and then failed to close leaves the
    marker behind, and the next pass must finish the close WITHOUT commenting
    a second time — so the plan carries which half of the write is still owed.
    """

    action: str
    observation_kind: str
    reason: str
    needs_comment: bool = True

    @property
    def holds(self) -> bool:
        return self.action == SYNC_ACTION_HOLD


def plan_outcome_sync(observation: ItemObservation) -> SyncPlan:
    """Read one observation as the next move, before the base is revalidated.

    The order is the safety property. An unreadable tracker holds rather than
    writing blind; kew's own marker on a closed item acknowledges rather than
    writing twice; kew's own marker on an OPEN item finishes the closure it
    started rather than declaring an open item synchronized; and an item
    somebody else already closed holds rather than claiming that closure as
    this judgment's effect.
    """
    if not observation.available:
        return SyncPlan(
            SYNC_ACTION_HOLD,
            OBSERVED_UNAVAILABLE,
            "the tracker could not be read, so the outcome stays sync-pending"
            + (f": {observation.detail}" if observation.detail else ""),
        )
    if observation.marker_found and observation.state == ITEM_CLOSED:
        return SyncPlan(
            SYNC_ACTION_ACK,
            OBSERVED_ALREADY_SYNCHRONIZED,
            "the item already carries this judgment's synchronization marker",
        )
    if observation.marker_found:
        # The marker landed and the closure did not: an earlier pass died
        # between the two writes. Acknowledging here would report a
        # synchronized outcome on an item anyone can see is still open.
        return SyncPlan(
            SYNC_ACTION_MUTATE,
            OBSERVED_APPLIED,
            "this judgment's marker is on the item but it is still open, so"
            " only the closure is owed",
            needs_comment=False,
        )
    if observation.state != ITEM_OPEN:
        return SyncPlan(
            SYNC_ACTION_HOLD,
            OBSERVED_CONFLICT,
            f"the item is {observation.state} and carries no marker from this"
            " judgment, so its state is somebody else's decision",
        )
    return SyncPlan(
        SYNC_ACTION_MUTATE,
        OBSERVED_APPLIED,
        "the item is open and unsynchronized",
    )


def stale_source_plan(revalidation: SourceRevalidation) -> SyncPlan:
    """The hold a failed source/base revalidation produces."""
    return SyncPlan(SYNC_ACTION_HOLD, OBSERVED_STALE_SOURCE, revalidation.detail)


def no_change_marker(idempotency_key: str) -> str:
    """The HTML comment a synchronized item carries, forever, exactly once."""
    return f"<!-- {idempotency_key} -->"


def no_change_comment_body(
    *, judgment: NoChangeJudgment, marker: str, actor: str | None = None
) -> str:
    """The note kew leaves on an item it is closing as a no-change.

    It says who decided and on what, because the whole difficulty of this
    outcome is that the artifact a reviewer would otherwise read does not
    exist.
    """
    decided_by = actor or judgment.decided_by or "policy"
    return (
        "**Closed with no code changes.**\n\n"
        f"kew's run produced no diff, and the outcome was authorized by"
        f" {decided_by} under the `{judgment.policy}` policy.\n\n"
        f"{judgment.decision_reason}\n\n"
        f"Judgment `{judgment.judgment_id}` (generation {judgment.generation}),"
        f" base `{judgment.base_sha}`, run `{judgment.run_id}`"
        f" attempt {judgment.attempt_number}.\n\n"
        f"{marker}\n"
    )


def no_change_rejection_body(
    *, judgment: NoChangeJudgment, reason: str, actor: str
) -> str:
    """The disposition note a rejection leaves. It closes nothing."""
    return (
        "**No-change outcome rejected.**\n\n"
        f"{actor} refused the no-change outcome kew recorded for this item:"
        f" {reason}\n\n"
        f"Judgment `{judgment.judgment_id}` (generation {judgment.generation})."
        " This item stays open and unsatisfied — nothing was closed, no"
        " dependent was unblocked, and no work was re-dispatched.\n"
    )
