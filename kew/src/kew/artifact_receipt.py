"""The artifact receipt: one immutable row that seals a lineage (I07).

By the time a package has been preserved (:mod:`kew.artifact_store`) and
promoted to a tree (:mod:`kew.publication_repository`), the host knows a chain
of facts: which source and which run/attempt, which trusted base, which
recoverable work package, which immutable tree, and how the tree was verified.
Individually each is a row somewhere. Together they are the answer to "what was
about to be published, and on the strength of what?" — and that answer is only
worth anything if it cannot be revised afterwards.

## What a seal is

* **One transaction, two writes.** The receipt row and its chained audit event
  are inserted together. A receipt without an event is an unattested claim; an
  event without a receipt attests nothing. Neither half can survive a crash
  alone.
* **A prefix, not a moment.** The receipt records ``through_seq`` — the id of
  its own seal event — and ``chain_head``, the entry hash of that row. Every
  audit event written before it is therefore covered: re-chaining the log to
  change any of them changes the head the receipt already recorded.
* **Immutable in the schema.** ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers
  refuse to let the row change at all. The digest is the second line of
  defence, for the database where somebody got past the first.

## What a seal is NOT

It is not the publication. Commit, head, branch and PR are E5's facts and they
arrive *after* this receipt, as chained attestations that reference it by
digest. :meth:`ArtifactReceiptStoreMixin.attest_artifact_receipt` is the only
way to add one, and it writes an audit event — never a column. That is the
whole reason the receipt can be immutable: nothing that happens later needs to
edit it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from kew.artifact_store import PACKAGE_ARTIFACT_KIND
from kew.audit_chain import ChainRow, entry_hash, verify_chain
from kew.run_store_audit import (
    _CHAIN_SELECT_TAIL,
    _normalize_event_ts,
    _row_to_chain_row,
)

if TYPE_CHECKING:
    from kew.artifact_store import PreservedPackage

_T = TypeVar("_T")

__all__ = [
    "RECEIPT_ATTESTED_EVENT",
    "RECEIPT_DIGEST_VERSION",
    "RECEIPT_SEALED_EVENT",
    "ArtifactLineage",
    "ArtifactReceipt",
    "ArtifactReceiptStoreMixin",
    "PromotedLike",
    "ReceiptAttestation",
    "ReceiptConflictError",
    "ReceiptError",
    "ReceiptMissingError",
    "ReceiptSealedError",
    "ReceiptValueError",
    "ReceiptVerification",
    "receipt_digest",
    "seal_package_receipt",
]

RECEIPT_SEALED_EVENT = "artifact_receipt_sealed"
"""The chained event a receipt is sealed by. One per receipt, ever."""

RECEIPT_ATTESTED_EVENT = "artifact_receipt_attested"
"""A later fact ABOUT a receipt — E5's commit/head/branch/PR, and after them
judgment and sync. Always an event, never an edit."""

RECEIPT_DIGEST_VERSION = 1
"""What a receipt digest covers. Bump when the preimage changes shape."""

_HEX = "0123456789abcdef"
_DIGEST_PREFIX = "sha256:"

_RECEIPT_COLS = (
    "run_id",
    "attempt_number",
    "source_kind",
    "source_id",
    "base_sha",
    "package_digest",
    "tree_sha",
    "verification",
    "locator",
    "receipt_digest",
    "through_seq",
    "chain_head",
    "sealed_at",
)

# Built once from module constants (column names only); every value is a bound
# parameter. bandit reports the f-string, so the suppression lives here.
_RECEIPT_SELECT = f"SELECT {', '.join(_RECEIPT_COLS)} FROM artifact_receipt"  # nosec B608
_RECEIPT_KEY = " WHERE run_id = ? AND attempt_number = ?"
_RECEIPT_BY_KEY = _RECEIPT_SELECT + _RECEIPT_KEY  # nosec B608
_RECEIPT_NAMES = ", ".join(_RECEIPT_COLS)
_RECEIPT_MARKS = ", ".join("?" * len(_RECEIPT_COLS))
_RECEIPT_INSERT = (
    f"INSERT INTO artifact_receipt ({_RECEIPT_NAMES})"  # nosec B608
    f" VALUES ({_RECEIPT_MARKS})"
)


class PromotedLike(Protocol):
    """The three facts a receipt needs from a promoted tree.

    Structural rather than an import of
    :class:`kew.publication_repository.PromotedTree`: that module shells out to
    git, which would put the runner registry on ``run_store``'s import graph
    and break the layered-dispatch contract. The receipt reads three strings
    off it and has no business depending on how they were produced.
    """

    @property
    def base_sha(self) -> str: ...

    @property
    def tree_sha(self) -> str: ...

    @property
    def verification(self) -> str: ...


class ReceiptError(RuntimeError):
    """Base class for every artifact-receipt refusal."""


class ReceiptValueError(ReceiptError):
    """A lineage link was not the kind of value a receipt may bind."""


class ReceiptConflictError(ReceiptError):
    """A different lineage is already sealed for this run and attempt."""


class ReceiptMissingError(ReceiptError):
    """There is no receipt to attest to. Never invented on the caller's behalf."""


class ReceiptSealedError(ReceiptError):
    """The receipt already carries a terminal attestation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReceiptValueError(message)


def _is_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(char in _HEX for char in value)
    )


def _is_package_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_DIGEST_PREFIX)
        and _is_oid(value.removeprefix(_DIGEST_PREFIX))
        and len(value.removeprefix(_DIGEST_PREFIX)) == 64
    )


@dataclass(frozen=True)
class ArtifactLineage:
    """Every link between a source and the tree the host would publish."""

    source_kind: str
    source_id: str
    run_id: str
    attempt_number: int
    base_sha: str
    package_digest: str
    tree_sha: str
    verification: str
    locator: str | None = None

    def validate(self) -> None:
        """Refuse a lineage with a broken link, BEFORE anything is written.

        A receipt is only as good as its weakest field: a ``tree_sha`` of ``""``
        or a ``package_digest`` that is not a digest would seal a statement
        nobody can check later, which is worse than sealing nothing.
        """
        _require(bool(self.source_kind), "a receipt needs a source kind")
        _require(bool(self.source_id), "a receipt needs a source id")
        _require(bool(self.run_id), "a receipt needs a run id")
        _require(
            isinstance(self.attempt_number, int) and self.attempt_number >= 1,
            f"attempt {self.attempt_number!r} is not an attempt number",
        )
        _require(_is_oid(self.base_sha), f"base {self.base_sha!r} is not a commit id")
        _require(_is_oid(self.tree_sha), f"tree {self.tree_sha!r} is not a tree id")
        _require(
            _is_package_digest(self.package_digest),
            f"package {self.package_digest!r} is not a sha256 digest",
        )
        _require(bool(self.verification), "a receipt needs a verification")

    def facts(self) -> dict[str, Any]:
        """The lineage as payload facts, in the digest's own order."""
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "run_id": self.run_id,
            "attempt_number": self.attempt_number,
            "base_sha": self.base_sha,
            "package_digest": self.package_digest,
            "tree_sha": self.tree_sha,
            "verification": self.verification,
            "locator": self.locator,
        }


def receipt_digest(lineage: ArtifactLineage, *, sealed_at: str) -> str:
    """A hex digest over the WHOLE lineage plus the moment it was sealed.

    Fixed-order array rather than a sorted dict, for the same reason
    :func:`kew.audit_chain.entry_hash` uses one: the order is part of the
    contract, so a field cannot be renamed into a different slot without the
    digest noticing.
    """
    preimage = json.dumps(
        [
            RECEIPT_DIGEST_VERSION,
            lineage.source_kind,
            lineage.source_id,
            lineage.run_id,
            lineage.attempt_number,
            lineage.base_sha,
            lineage.package_digest,
            lineage.tree_sha,
            lineage.verification,
            lineage.locator,
            sealed_at,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArtifactReceipt:
    """One sealed ``artifact_receipt`` row."""

    run_id: str
    attempt_number: int
    source_kind: str
    source_id: str
    base_sha: str
    package_digest: str
    tree_sha: str
    verification: str
    locator: str | None
    receipt_digest: str
    through_seq: int
    chain_head: str
    sealed_at: str

    @property
    def lineage(self) -> ArtifactLineage:
        return ArtifactLineage(
            source_kind=self.source_kind,
            source_id=self.source_id,
            run_id=self.run_id,
            attempt_number=self.attempt_number,
            base_sha=self.base_sha,
            package_digest=self.package_digest,
            tree_sha=self.tree_sha,
            verification=self.verification,
            locator=self.locator,
        )


@dataclass(frozen=True)
class ReceiptAttestation:
    """A later fact recorded about a receipt, read back off the chain."""

    seq: int
    kind: str
    terminal: bool
    timestamp: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReceiptVerification:
    """Whether a receipt still stands, and if not, where it stopped standing."""

    ok: bool
    detail: str = ""


def _row_to_receipt(row: sqlite3.Row) -> ArtifactReceipt:
    return ArtifactReceipt(**{col: row[col] for col in _RECEIPT_COLS})


def _iso(value: datetime | str) -> str:
    return _normalize_event_ts(value if isinstance(value, str) else value.isoformat())


class ArtifactReceiptStoreMixin:
    """The ``artifact_receipt`` surface of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore.
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

        def read_chain_rows(self) -> list[ChainRow]: ...

    def seal_artifact_receipt(
        self, *, lineage: ArtifactLineage, sealed_at: datetime | str
    ) -> ArtifactReceipt:
        """Seal *lineage* and the audit prefix it closes, in one transaction.

        Idempotent for an identical LINEAGE: re-sealing returns the receipt
        that already exists and appends no second event, so a retry after a
        crash converges instead of forking the evidence. The retry's clock is
        deliberately not part of that comparison — the first seal is the one
        that happened, and re-stamping it would move the very fact the digest
        binds. A *different* lineage for the same run and attempt is a
        conflict, never an overwrite.
        """
        lineage.validate()
        stamp = _iso(sealed_at)
        digest = receipt_digest(lineage, sealed_at=stamp)
        return self._with_busy_retry(
            "seal_artifact_receipt",
            lambda: self._seal(lineage, stamp=stamp, digest=digest),
        )

    def _seal(
        self, lineage: ArtifactLineage, *, stamp: str, digest: str
    ) -> ArtifactReceipt:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._read_receipt(lineage.run_id, lineage.attempt_number)
                if existing is not None:
                    self._conn.commit()
                    return _same_or_conflict(existing, lineage)
                receipt = self._insert_sealed(lineage, stamp=stamp, digest=digest)
                self._conn.commit()
                return receipt
            except BaseException:
                self._conn.rollback()
                raise

    def _insert_sealed(
        self, lineage: ArtifactLineage, *, stamp: str, digest: str
    ) -> ArtifactReceipt:
        """The two writes that make a seal, inside the caller's transaction.

        The event goes first so its id can be the receipt's ``through_seq`` and
        its entry hash the receipt's ``chain_head`` — the receipt seals a prefix
        that ends with its own attestation, which is what makes "everything up
        to here" a checkable statement rather than a phrase.
        """
        payload = {**lineage.facts(), "receipt_digest": digest, "sealed_at": stamp}
        seq = self._chained_insert_in_transaction(
            (
                stamp,
                RECEIPT_SEALED_EVENT,
                lineage.run_id,
                None,
                lineage.source_id,
                "host",
                "artifact_receipt",
                None,
                None,
                json.dumps(payload),
                None,
            )
        )
        if seq is None:  # pragma: no cover - sqlite always reports a lastrowid
            raise ReceiptError("the seal event was not written")
        head = entry_hash(self._chain_tail())
        receipt = ArtifactReceipt(
            **lineage.facts(),
            receipt_digest=digest,
            through_seq=seq,
            chain_head=head,
            sealed_at=stamp,
        )
        self._execute(
            _RECEIPT_INSERT,
            tuple(getattr(receipt, col) for col in _RECEIPT_COLS),
        )
        return receipt

    def _chain_tail(self) -> ChainRow:
        row = self._conn.execute(_CHAIN_SELECT_TAIL).fetchone()  # nosemgrep
        if row is None:  # pragma: no cover - the seal event was just inserted
            raise ReceiptError("the audit chain lost the seal event")
        return _row_to_chain_row(row)

    def _read_receipt(self, run_id: str, attempt_number: int) -> ArtifactReceipt | None:
        row = self._conn.execute(  # nosemgrep
            _RECEIPT_BY_KEY, (run_id, attempt_number)
        ).fetchone()
        return None if row is None else _row_to_receipt(row)

    def get_artifact_receipt(
        self, *, run_id: str, attempt_number: int
    ) -> ArtifactReceipt | None:
        """The sealed receipt for one attempt, or None if nothing was sealed."""
        return self._read_receipt(run_id, attempt_number)

    def list_artifact_receipts(
        self, *, run_id: str | None = None
    ) -> list[ArtifactReceipt]:
        """Receipts in seal order, optionally narrowed to one run."""
        where = " WHERE run_id = ?" if run_id is not None else ""
        params: tuple[Any, ...] = (run_id,) if run_id is not None else ()
        sql = _RECEIPT_SELECT + where + " ORDER BY through_seq ASC"
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [_row_to_receipt(row) for row in rows]

    def attest_artifact_receipt(
        self,
        *,
        run_id: str,
        attempt_number: int,
        kind: str,
        timestamp: datetime | str,
        payload: dict[str, Any] | None = None,
        terminal: bool = False,
    ) -> int:
        """Record a later fact ABOUT a sealed receipt. Never edits one.

        The receipt's digest and ``through_seq`` are stamped into the payload
        by this method, overriding anything the caller passed under those keys:
        an attestation that could name a different receipt than the one it was
        written against would be a way to launder a lineage.
        """
        _require(bool(kind), "an attestation needs a kind")
        stamp = _iso(timestamp)
        return self._with_busy_retry(
            "attest_artifact_receipt",
            lambda: self._attest(
                run_id, attempt_number, kind, stamp, payload or {}, terminal
            ),
        )

    def _attest(
        self,
        run_id: str,
        attempt_number: int,
        kind: str,
        stamp: str,
        payload: dict[str, Any],
        terminal: bool,
    ) -> int:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                receipt = self._read_receipt(run_id, attempt_number)
                if receipt is None:
                    raise ReceiptMissingError(
                        f"no receipt is sealed for {run_id} attempt {attempt_number}"
                    )
                self._refuse_after_terminal(run_id, attempt_number)
                seq = self._chained_insert_in_transaction(
                    _attestation_values(receipt, kind, stamp, payload, terminal)
                )
                self._conn.commit()
                return int(seq or 0)
            except BaseException:
                self._conn.rollback()
                raise

    def _refuse_after_terminal(self, run_id: str, attempt_number: int) -> None:
        for attestation in self._attestations(run_id, attempt_number):
            if attestation.terminal:
                raise ReceiptSealedError(
                    f"{run_id} attempt {attempt_number} already carries a terminal"
                    f" attestation ({attestation.kind})"
                )

    def artifact_receipt_attestations(
        self, *, run_id: str, attempt_number: int
    ) -> list[ReceiptAttestation]:
        """Later facts recorded about this receipt, in chain order."""
        return self._attestations(run_id, attempt_number)

    def _attestations(
        self, run_id: str, attempt_number: int
    ) -> list[ReceiptAttestation]:
        rows = self._conn.execute(
            "SELECT id, timestamp, payload FROM audit_events"
            " WHERE event = ? AND run_id = ? ORDER BY id ASC",
            (RECEIPT_ATTESTED_EVENT, run_id),
        ).fetchall()
        found: list[ReceiptAttestation] = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            if payload.get("attempt_number") != attempt_number:
                continue
            found.append(
                ReceiptAttestation(
                    seq=int(row["id"]),
                    kind=str(payload.get("attestation_kind", "")),
                    terminal=bool(payload.get("terminal", False)),
                    timestamp=str(row["timestamp"]),
                    payload=payload,
                )
            )
        return found

    def verify_artifact_receipt(
        self, *, run_id: str, attempt_number: int
    ) -> ReceiptVerification:
        """Recompute a receipt from the database rather than believing it.

        Three questions, in the order they can fail: does the row still hash to
        the digest it was sealed with, does the sealed event still hash to the
        recorded head, and does the audit prefix through ``through_seq`` still
        chain? A "yes" to all three is the only reading of this receipt as
        evidence.
        """
        receipt = self._read_receipt(run_id, attempt_number)
        if receipt is None:
            return ReceiptVerification(
                False, f"no receipt is sealed for {run_id} attempt {attempt_number}"
            )
        actual = receipt_digest(receipt.lineage, sealed_at=receipt.sealed_at)
        if actual != receipt.receipt_digest:
            return ReceiptVerification(
                False, f"the receipt row does not match its own digest ({actual})"
            )
        return self._verify_prefix(receipt)

    def _verify_prefix(self, receipt: ArtifactReceipt) -> ReceiptVerification:
        prefix = [
            row for row in self.read_chain_rows() if row.id <= receipt.through_seq
        ]
        sealed = [row for row in prefix if row.id == receipt.through_seq]
        if not sealed:
            return ReceiptVerification(
                False, f"the seal event {receipt.through_seq} is gone from the chain"
            )
        result = verify_chain(prefix)
        if not result.ok:
            return ReceiptVerification(
                False, f"the sealed audit prefix breaks at row {result.broken_at_id}"
            )
        if result.head_hash != receipt.chain_head:
            return ReceiptVerification(
                False, "the sealed audit prefix no longer hashes to the recorded head"
            )
        return ReceiptVerification(True)


def _same_or_conflict(
    existing: ArtifactReceipt, lineage: ArtifactLineage
) -> ArtifactReceipt:
    if existing.lineage != lineage:
        raise ReceiptConflictError(
            f"{existing.run_id} attempt {existing.attempt_number} is already"
            " sealed with a different lineage"
        )
    return existing


def _attestation_values(
    receipt: ArtifactReceipt,
    kind: str,
    stamp: str,
    payload: dict[str, Any],
    terminal: bool,
) -> tuple[Any, ...]:
    """The audit-row values for one attestation. Bound facts win over *payload*."""
    facts = {
        **payload,
        "attestation_kind": kind,
        "terminal": terminal,
        "run_id": receipt.run_id,
        "attempt_number": receipt.attempt_number,
        "receipt_digest": receipt.receipt_digest,
        "through_seq": receipt.through_seq,
    }
    return (
        stamp,
        RECEIPT_ATTESTED_EVENT,
        receipt.run_id,
        None,
        receipt.source_id,
        "host",
        "artifact_receipt",
        None,
        None,
        json.dumps(facts),
        None,
    )


def seal_package_receipt(
    store: Any,
    *,
    package: PreservedPackage,
    promoted: PromotedLike,
    source_kind: str,
    source_id: str,
    run_id: str,
    attempt_number: int,
    sealed_at: datetime | str,
) -> ArtifactReceipt:
    """Index a durable placement, then seal the lineage that spans it.

    The ``run_artifact`` row is written first and separately, on purpose: it is
    the metadata index over bytes that already exist on disk, and it is
    idempotent, so a crash between the two steps leaves an indexed placement
    with no receipt — recoverable — rather than a receipt pointing at an
    unindexed one.
    """
    store.record_artifact(
        run_id=run_id,
        attempt_number=attempt_number,
        kind=PACKAGE_ARTIFACT_KIND,
        digest=package.digest,
        byte_length=package.byte_length,
        locator=package.locator,
        created_at=sealed_at,
    )
    lineage = ArtifactLineage(
        source_kind=source_kind,
        source_id=source_id,
        run_id=run_id,
        attempt_number=attempt_number,
        base_sha=promoted.base_sha,
        package_digest=package.digest,
        tree_sha=promoted.tree_sha,
        verification=promoted.verification,
        locator=package.locator,
    )
    sealed: ArtifactReceipt = store.seal_artifact_receipt(
        lineage=lineage, sealed_at=sealed_at
    )
    return sealed
