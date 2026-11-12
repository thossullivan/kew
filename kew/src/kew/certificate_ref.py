"""The journey-certificate evidence carrier on ``refs/notes/kew-certificates``.

Certification is the third orthogonal truth in kew's route story, beside the
configured capability profile and current execution validity. It has its own
carrier for one reason: **the subject and the evidence are different objects.**
The envelope names an exact kew SHA, but it lives on a separate evidence ref
outside that tree, so attaching or updating evidence never changes the SHA
being certified and no file inside a commit can talk its own build into being
certified.

Three rules give the carrier its value, and every function here exists to keep
one of them true:

* **Append-only, compare-and-swap, fast-forward-only.** Each subject owns an
  immutable manifest of typed envelopes; each entry carries its canonical
  digest and its predecessor. A write is a new commit whose parent is the ref
  head the writer read, applied with git's own old-value check. A rewrite,
  replay, deletion, or write against a moved head is refused rather than
  merged over the top of prior evidence.
* **``certification_ready`` is not ``certified``.** Build acceptance and a
  journey certificate are distinct envelope kinds. Only a
  :data:`KIND_CERTIFICATE` envelope bound to an exact lane can resolve to
  ``certified``, and only once its append is recorded in the audit chain AND
  covered by a current anchor. Recertification and suspension append status
  envelopes; they never replace the certificate they follow.
* **Absent evidence fails closed.** An unfetched, unreadable, stale, or
  invalid carrier resolves to ``unknown`` with a non-available observation —
  never to ``uncertified`` (which is a real, readable answer meaning the
  carrier genuinely holds nothing for this subject) and never to a preserved
  ``certified`` claim.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404 - every call goes through subprocess_utils.run_bytes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from kew import work_dirs
from kew.audit_chain import Anchor, verify_anchor_chain
from kew.publication_apply import trusted_git_env
from kew.subprocess_utils import run_bytes

#: The one ref the resolver consumes. It is explicitly fetched, never inferred.
CERTIFICATE_REF: Final = "refs/notes/kew-certificates"

ENVELOPE_VERSION: Final = 1

#: Deterministic build acceptance. Records that a candidate build passed its
#: own gates — which is a release status, not a customer-journey claim.
KIND_READY: Final = "certification_ready"
#: A journey certificate bound to an exact lane and live trace.
KIND_CERTIFICATE: Final = "journey_certificate"
#: A later status append over an existing certificate.
KIND_STATUS: Final = "certificate_status"

#: Which statuses each kind may carry. A kind cannot borrow another's
#: vocabulary, so no envelope can claim ``certified`` except the one type of
#: envelope that is allowed to mean it.
_STATUS_BY_KIND: Final[dict[str, frozenset[str]]] = {
    KIND_READY: frozenset({"build_accepted", "build_rejected"}),
    KIND_CERTIFICATE: frozenset({"certified"}),
    KIND_STATUS: frozenset({"recertification_due", "suspended"}),
}

#: How old a reading of the carrier may be before it stops counting as current.
DEFAULT_MAX_AGE: Final = timedelta(hours=24)

_MANIFEST_VERSION: Final = 1
_ENVELOPE_DIR: Final = "envelopes"
_AUDIT_EVENT: Final = "certificate_appended"
_NEVER_FETCHED: Final = "the certificate evidence ref was never fetched"
_UNREADABLE: Final = "the certificate evidence ref could not be read"

#: Distinguishes "the caller said nothing" from "the caller said None", which
#: for a ref head is the meaningful assertion *the ref must not exist yet*.
_UNSET: Final = "\x00unset"


class CertificateRefError(RuntimeError):
    """Base for every refusal to read or write the certificate carrier."""


class CertificateConflict(CertificateRefError):
    """The ref moved under the writer. Nothing was overwritten."""


class CertificateRefused(CertificateRefError):
    """The write (or the evidence read back) violates an append-only rule."""


# --------------------------------------------------------------------------- #
# Envelopes and manifests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CertificateEnvelope:
    """One immutable typed statement about one exact subject SHA."""

    kind: str
    subject: str
    status: str
    lane: str
    fingerprint: str
    issued_at: str
    actor: str
    version: int = ENVELOPE_VERSION
    body: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "subject": self.subject,
            "status": self.status,
            "lane": self.lane,
            "fingerprint": self.fingerprint,
            "issued_at": self.issued_at,
            "actor": self.actor,
            "body": dict(self.body),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CertificateEnvelope:
        try:
            return cls(
                kind=str(data["kind"]),
                subject=str(data["subject"]),
                status=str(data["status"]),
                lane=str(data["lane"]),
                fingerprint=str(data["fingerprint"]),
                issued_at=str(data["issued_at"]),
                actor=str(data["actor"]),
                version=int(data["version"]),
                body=dict(data.get("body") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CertificateRefused(f"malformed envelope: {exc}") from exc

    def canonical(self) -> bytes:
        """The exact bytes that are hashed and stored. Stable across writers."""
        return json.dumps(
            self.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    """One append: the envelope, its identity, and what it followed."""

    digest: str
    oid: str
    predecessor: str | None
    recorded_at: str
    envelope: CertificateEnvelope

    @property
    def kind(self) -> str:
        return self.envelope.kind

    @property
    def status(self) -> str:
        return self.envelope.status

    def to_json(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "oid": self.oid,
            "predecessor": self.predecessor,
            "recorded_at": self.recorded_at,
            "envelope": self.envelope.to_json(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ManifestEntry:
        try:
            envelope = CertificateEnvelope.from_json(data["envelope"])
            predecessor = data["predecessor"]
            return cls(
                digest=str(data["digest"]),
                oid=str(data["oid"]),
                predecessor=None if predecessor is None else str(predecessor),
                recorded_at=str(data["recorded_at"]),
                envelope=envelope,
            )
        except (KeyError, TypeError) as exc:
            raise CertificateRefused(f"malformed manifest entry: {exc}") from exc


@dataclass(frozen=True)
class Manifest:
    """Every envelope recorded for one subject, oldest first."""

    subject: str
    entries: tuple[ManifestEntry, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": _MANIFEST_VERSION,
            "subject": self.subject,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @property
    def tail(self) -> ManifestEntry | None:
        return self.entries[-1] if self.entries else None


@dataclass(frozen=True)
class AppendResult:
    """What one accepted append did to the carrier."""

    ref: str
    subject: str
    digest: str
    oid: str
    predecessor: str | None
    ref_before: str | None
    ref_after: str


@dataclass(frozen=True)
class CertificateReading:
    """One resolved answer about one subject, with the honesty of its source.

    ``state`` and ``availability`` are separate on purpose. ``uncertified`` is
    only ever reported under ``available``: it is the answer a successful read
    of a carrier that holds nothing gives. Everything unread, unreadable,
    stale, or invalid is ``unknown`` — not certified, and not uncertified.
    """

    state: str  # "certified" | "recertification_due" | "suspended"
    #                       | "uncertified" | "unknown"
    availability: str  # "available" | "stale" | "unavailable"
    detail: str
    subject: str | None = None
    lane: str | None = None
    certified_at: str | None = None
    digest: str | None = None
    evidence_ref: str | None = None
    head: str | None = None


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def _run(
    repo: Path,
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return run_bytes(
        ["git", "-C", str(repo), *args], stdin=stdin, env=env or trusted_git_env()
    )


def _out(
    repo: Path,
    args: Sequence[str],
    *,
    env: dict[str, str],
    stdin: bytes | None = None,
    what: str,
) -> str:
    result = _run(repo, args, env=env, stdin=stdin)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise CertificateRefError(f"{what} failed: {detail or 'unknown git error'}")
    return (result.stdout or b"").decode("utf-8", errors="replace").strip()


def ref_head(repo: Path, ref: str = CERTIFICATE_REF) -> str | None:
    """The commit the evidence ref points at, or ``None`` if it is not there."""
    result = _run(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if result.returncode != 0:
        return None
    return (result.stdout or b"").decode("ascii", errors="replace").strip() or None


def _read_blob(repo: Path, head: str, path: str) -> bytes | None:
    result = _run(repo, ["cat-file", "blob", f"{head}:{path}"])
    return result.stdout if result.returncode == 0 else None


def _commit_env(now: datetime) -> dict[str, str]:
    """A deterministic, fully host-owned identity for the evidence commit."""
    stamp = now.isoformat()
    return trusted_git_env(
        GIT_AUTHOR_NAME="kew",
        GIT_AUTHOR_EMAIL="kew@invalid",
        GIT_COMMITTER_NAME="kew",
        GIT_COMMITTER_EMAIL="kew@invalid",
        GIT_AUTHOR_DATE=stamp,
        GIT_COMMITTER_DATE=stamp,
    )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _parse_manifest(subject: str, blob: bytes) -> Manifest:
    """Decode and *re-verify* a manifest. A manifest that fails here is not
    a weaker manifest — it is evidence the carrier was tampered with, and the
    caller must treat it as unreadable rather than as a smaller truth."""
    try:
        document = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CertificateRefused(f"unreadable manifest: {exc}") from exc
    if not isinstance(document, dict):
        raise CertificateRefused("manifest is not an object")
    if document.get("version") != _MANIFEST_VERSION:
        raise CertificateRefused(
            f"unsupported manifest version {document.get('version')!r}"
        )
    if document.get("subject") != subject:
        raise CertificateRefused("manifest subject does not match the path it lives at")
    entries = tuple(ManifestEntry.from_json(e) for e in document.get("entries") or ())
    _verify_chain(subject, entries)
    return Manifest(subject=subject, entries=entries)


def _verify_chain(subject: str, entries: Sequence[ManifestEntry]) -> None:
    """Each entry must hash to its own digest and name the one before it."""
    expected: str | None = None
    for index, entry in enumerate(entries):
        if entry.envelope.subject != subject:
            raise CertificateRefused(f"entry {index} names another subject")
        if entry.envelope.digest != entry.digest:
            raise CertificateRefused(f"entry {index} does not match its digest")
        if entry.predecessor != expected:
            raise CertificateRefused(f"entry {index} has a broken predecessor link")
        expected = entry.digest


def read_manifest(
    repo: Path, subject: str, *, ref: str = CERTIFICATE_REF
) -> Manifest | None:
    """Every envelope recorded for *subject*, or ``None`` when there are none.

    Raises :class:`CertificateRefused` when the manifest exists but does not
    re-verify — an invalid manifest is never quietly downgraded to absent.
    """
    head = ref_head(repo, ref)
    if head is None:
        return None
    blob = _read_blob(repo, head, subject)
    return None if blob is None else _parse_manifest(subject, blob)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _validate(envelope: CertificateEnvelope, subject: str) -> None:
    if envelope.version != ENVELOPE_VERSION:
        raise CertificateRefused(f"unsupported envelope version {envelope.version!r}")
    allowed = _STATUS_BY_KIND.get(envelope.kind)
    if allowed is None:
        raise CertificateRefused(f"unknown envelope kind {envelope.kind!r}")
    if envelope.status not in allowed:
        raise CertificateRefused(
            f"status {envelope.status!r} is not valid for kind {envelope.kind!r}"
        )
    if envelope.subject != subject:
        raise CertificateRefused(
            f"envelope subject {envelope.subject!r} is not the subject being written"
        )
    if not envelope.lane or not envelope.fingerprint:
        raise CertificateRefused("an envelope must name its lane and fingerprint")


def _next_predecessor(
    manifest: Manifest | None, envelope: CertificateEnvelope, claimed: str | None
) -> str | None:
    """The digest this append follows, having refused every replay and rewrite."""
    entries = manifest.entries if manifest else ()
    if any(entry.digest == envelope.digest for entry in entries):
        raise CertificateRefused("this envelope is already recorded for this subject")
    tail = entries[-1].digest if entries else None
    if claimed != _UNSET and claimed != tail:
        raise CertificateRefused(
            f"predecessor {claimed!r} is not the current tail {tail!r}"
        )
    return tail


def _existing(repo: Path, subject: str, ref: str) -> Manifest | None:
    try:
        return read_manifest(repo, subject, ref=ref)
    except CertificateRefused as exc:
        raise CertificateRefused(
            f"refusing to append over invalid evidence: {exc}"
        ) from exc


def append_certificate(
    repo: Path,
    envelope: CertificateEnvelope,
    *,
    ledger: Any | None = None,
    now: datetime | None = None,
    subject: str | None = None,
    ref: str = CERTIFICATE_REF,
    expected_head: str | None = _UNSET,
    predecessor: str | None = _UNSET,
) -> AppendResult:
    """Append *envelope* to *subject*'s manifest on *ref*, or refuse.

    The ref advances by exactly one commit whose parent is the head the writer
    read, applied with git's own old-value check, so a concurrent writer
    conflicts instead of losing evidence. The audit row is written **after**
    the ref moves: a crash between them leaves evidence that no resolver will
    call ``certified``, which is the safe side of that race.
    """
    moment = now or datetime.now(UTC)
    target = subject or envelope.subject
    _validate(envelope, target)
    before = ref_head(repo, ref)
    if expected_head != _UNSET and expected_head != before:
        raise CertificateConflict(
            f"{ref} is at {before!r}, not the expected {expected_head!r}"
        )
    manifest = _existing(repo, target, ref)
    prior = _next_predecessor(manifest, envelope, predecessor)
    after, oid = _commit_append(
        repo, target, envelope, manifest, prior, before, moment, ref
    )
    result = AppendResult(
        ref=ref,
        subject=target,
        digest=envelope.digest,
        oid=oid,
        predecessor=prior,
        ref_before=before,
        ref_after=after,
    )
    _bind_to_audit(ledger, envelope, result, moment)
    return result


def _commit_append(
    repo: Path,
    subject: str,
    envelope: CertificateEnvelope,
    manifest: Manifest | None,
    prior: str | None,
    before: str | None,
    moment: datetime,
    ref: str,
) -> tuple[str, str]:
    """Write the objects, build the successor commit, and CAS the ref."""
    env = _commit_env(moment)
    with work_dirs.work_tempdir("kew-certificate-") as scratch:
        env["GIT_INDEX_FILE"] = str(Path(scratch) / "index")
        if before is None:
            _out(repo, ["read-tree", "--empty"], env=env, what="read-tree --empty")
        else:
            _out(repo, ["read-tree", before], env=env, what="read-tree")
        oid = _write_blob(repo, envelope.canonical(), env)
        entry = ManifestEntry(
            digest=envelope.digest,
            oid=oid,
            predecessor=prior,
            recorded_at=moment.isoformat(),
            envelope=envelope,
        )
        entries = (*(manifest.entries if manifest else ()), entry)
        document = Manifest(subject=subject, entries=entries).to_json()
        blob = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        _stage(repo, subject, _write_blob(repo, blob, env), env)
        _stage(repo, f"{_ENVELOPE_DIR}/{envelope.digest}", oid, env)
        commit = _build_commit(repo, envelope, before, env)
        _cas(repo, ref, commit, before, env)
    return commit, oid


def _write_blob(repo: Path, payload: bytes, env: dict[str, str]) -> str:
    return _out(
        repo,
        ["hash-object", "-w", "-t", "blob", "--stdin"],
        env=env,
        stdin=payload,
        what="hash-object",
    )


def _stage(repo: Path, path: str, oid: str, env: dict[str, str]) -> None:
    _out(
        repo,
        ["update-index", "--add", "--cacheinfo", f"100644,{oid},{path}"],
        env=env,
        what=f"update-index for {path!r}",
    )


def _build_commit(
    repo: Path, envelope: CertificateEnvelope, before: str | None, env: dict[str, str]
) -> str:
    tree = _out(repo, ["write-tree"], env=env, what="write-tree")
    message = f"certificate: {envelope.kind} {envelope.status} {envelope.subject}"
    args = ["commit-tree", tree, "-m", message]
    if before is not None:
        args += ["-p", before]
    return _out(repo, args, env=env, what="commit-tree")


def _cas(
    repo: Path, ref: str, commit: str, before: str | None, env: dict[str, str]
) -> None:
    """Advance *ref* only if it is still where the writer last saw it."""
    result = _run(repo, ["update-ref", ref, commit, before or ""], env=env)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise CertificateConflict(f"{ref} moved during the append: {detail}")


def _bind_to_audit(
    ledger: Any | None,
    envelope: CertificateEnvelope,
    result: AppendResult,
    moment: datetime,
) -> None:
    """Record the append independently in the audit chain.

    Everything attested here rides in ``payload``: the chain's hashed slots are
    grandfathered envelope fields and a new fact never becomes one.
    """
    if ledger is None:
        return
    ledger.append_audit_event(
        timestamp=moment.isoformat(),
        event=_AUDIT_EVENT,
        actor_type="host",
        actor_id=envelope.actor,
        payload={
            "subject": result.subject,
            "kind": envelope.kind,
            "status": envelope.status,
            "lane": envelope.lane,
            "fingerprint": envelope.fingerprint,
            "digest": result.digest,
            "oid": result.oid,
            "predecessor": result.predecessor,
            "ref": result.ref,
            "ref_before": result.ref_before,
            "ref_after": result.ref_after,
            "actor": envelope.actor,
            "recorded_at": moment.isoformat(),
        },
    )


# --------------------------------------------------------------------------- #
# Resolving — fail-closed in both directions
# --------------------------------------------------------------------------- #


def _unknown(
    detail: str, subject: str, *, availability: str, head: str | None = None
) -> CertificateReading:
    return CertificateReading(
        state="unknown",
        availability=availability,
        detail=detail,
        subject=subject,
        head=head,
    )


def resolve_certificate(
    repo: Path,
    subject: str,
    *,
    ref: str = CERTIFICATE_REF,
    audit_rows: Sequence[Any] = (),
    anchors: Sequence[Anchor] = (),
    fetched_at: datetime | None = None,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> CertificateReading:
    """Resolve what the carrier says about *subject*, refusing to guess.

    *fetched_at* is when this checkout's copy of *ref* was last explicitly
    fetched; ``None`` means nobody fetched it, which is unavailable evidence
    rather than an absence of certification.
    """
    moment = now or datetime.now(UTC)
    if fetched_at is None:
        return _unknown(_NEVER_FETCHED, subject, availability="unavailable")
    head = ref_head(repo, ref)
    if head is None:
        return _unknown(_UNREADABLE, subject, availability="unavailable")
    blob = _read_blob(repo, head, subject)
    if blob is None:
        return CertificateReading(
            state="uncertified",
            availability="available",
            detail="no certificate evidence names this subject",
            subject=subject,
            head=head,
        )
    try:
        manifest = _parse_manifest(subject, blob)
    except CertificateRefused as exc:
        return _unknown(
            f"the certificate evidence is invalid: {exc}",
            subject,
            availability="unavailable",
            head=head,
        )
    if moment - fetched_at > max_age:
        return _unknown(
            "the certificate evidence reading is stale and is not a current claim",
            subject,
            availability="stale",
            head=head,
        )
    return _verdict(manifest, ref, head, audit_rows, anchors)


def _verdict(
    manifest: Manifest,
    ref: str,
    head: str,
    audit_rows: Sequence[Any],
    anchors: Sequence[Anchor],
) -> CertificateReading:
    tail = manifest.tail
    if tail is None:
        return CertificateReading(
            state="uncertified",
            availability="available",
            detail="no certificate evidence names this subject",
            subject=manifest.subject,
            head=head,
        )
    # Re-validate the tail envelope's (version, kind, status) at RESOLVE time,
    # not only at write time. Write validation is the primary gate, but a tail
    # this resolver cannot fully read must fail closed rather than fall through
    # to `_certified_or_not` and certify. Otherwise "won't certify" would rest
    # entirely on the audit chain never being forged; this makes it hold even
    # if it were. Mirrors `_validate` (the write-time check).
    if tail.envelope.version != ENVELOPE_VERSION:
        return _unknown(
            f"the certificate envelope version {tail.envelope.version!r} is not"
            " readable by this resolver",
            manifest.subject,
            availability="unavailable",
            head=head,
        )
    if tail.status not in _STATUS_BY_KIND.get(tail.kind, frozenset()):
        return _unknown(
            f"the certificate envelope status {tail.status!r} is not a"
            f" recognized status for a {tail.kind!r} envelope",
            manifest.subject,
            availability="unavailable",
            head=head,
        )
    if tail.kind == KIND_READY:
        return CertificateReading(
            state="uncertified",
            availability="available",
            detail=(
                "certification_ready records deterministic build acceptance; "
                "it is a build status and never a journey certificate"
            ),
            subject=manifest.subject,
            lane=tail.envelope.lane,
            digest=tail.digest,
            head=head,
        )
    if tail.kind == KIND_STATUS:
        return CertificateReading(
            state=tail.status,
            availability="available",
            detail=f"a later {tail.status} envelope stands over the certificate",
            subject=manifest.subject,
            lane=tail.envelope.lane,
            digest=tail.digest,
            head=head,
        )
    return _certified_or_not(manifest, tail, ref, head, audit_rows, anchors)


def _certified_or_not(
    manifest: Manifest,
    tail: ManifestEntry,
    ref: str,
    head: str,
    audit_rows: Sequence[Any],
    anchors: Sequence[Anchor],
) -> CertificateReading:
    """A journey certificate counts only once the chain and an anchor say so."""
    row_id = _audit_binding(tail.digest, audit_rows)
    if row_id is None:
        return _unknown(
            "the certificate append is not bound to the audit chain",
            manifest.subject,
            availability="unavailable",
            head=head,
        )
    if not _anchored(row_id, anchors):
        return _unknown(
            "the certificate append is not covered by a current audit anchor",
            manifest.subject,
            availability="unavailable",
            head=head,
        )
    return CertificateReading(
        state="certified",
        availability="available",
        detail=(
            f"a journey certificate covers lane {tail.envelope.lane} — "
            "past evidence, not current reachability"
        ),
        subject=manifest.subject,
        lane=tail.envelope.lane,
        certified_at=tail.envelope.issued_at,
        digest=tail.digest,
        evidence_ref=f"{ref}#{tail.digest[:12]}",
        head=head,
    )


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _payload_of(row: Any) -> dict[str, Any]:
    payload = _field(row, "payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _audit_binding(digest: str, rows: Sequence[Any]) -> int | None:
    """The id of the audit row that recorded this exact append, if any."""
    for row in rows:
        if _field(row, "event") != _AUDIT_EVENT:
            continue
        if _payload_of(row).get("digest") == digest:
            row_id = int(_field(row, "id") or 0)
            # A real ledger row always carries a positive id; a missing/zero id
            # would make _anchored accept any anchor (id >= 0), so treat it as
            # no binding rather than a degraded one.
            return row_id if row_id > 0 else None
    return None


def _anchored(row_id: int, anchors: Sequence[Anchor]) -> bool:
    """Whether a verifiable anchor already covers the row at *row_id*."""
    if not anchors:
        return False
    if not verify_anchor_chain(list(anchors)).ok:
        return False
    return any(int(getattr(anchor, "id", 0) or 0) >= row_id for anchor in anchors)
