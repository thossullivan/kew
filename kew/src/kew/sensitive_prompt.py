"""The exact compiled prompt as an owner-only local artifact (I06).

A compiled dispatch prompt interpolates the whole attacker-reachable spec, the
repo map, and whatever the operator appended. Those bytes are exactly what a
reviewer sometimes needs and exactly what must never be copied into a managed
surface, so this module keeps them in one place and one place only:

* **Outside SQLite.** The bytes live under ``.kew/private/prompts`` — a
  directory the store never reads, the audit ledger never quotes, and
  :func:`kew.backup.create_backup` never stages (it copies ``dispatch.db``,
  ``kew.toml`` and the flat ``dispatch-*`` log files beside the DB, none of
  which is this subtree).
* **Owner-only, no-follow.** Creation goes through ``O_CREAT | O_EXCL |
  O_NOFOLLOW`` at mode ``0o600`` inside a ``0o700`` directory, so a planted
  symlink cannot redirect the write and a second writer cannot clobber the
  first. Reads cross the same ``O_NOFOLLOW`` boundary.
* **Digest-addressed, never digest-named.** The stored *reference* is a digest
  over the exact UTF-8 bytes; the stored *filename* is an opaque key derived
  from ``(run_id, attempt_number)``, so a hostile run id can never steer the
  path and the digest is not readable out of a directory listing.
* **Retained, then purged.** Retention is terminal-plus-30-days
  (:data:`RETENTION_DAYS`). Purging removes the bytes and leaves the metadata
  row behind, so a later reader sees *unavailable* rather than *empty*.

Everything a payload, receipt, list, export or certificate is allowed to carry
is what :meth:`RetainedPrompt.artifact_facts` returns: kind, digest, byte
length, storage state. Nothing here formats the bytes for display; reading them
back is a deliberate call to :func:`read_prompt` with the digest you expect.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

PROMPT_ARTIFACT_KIND = "compiled_prompt"
"""The artifact kind these bytes are recorded under."""

PROTECTED_SUBPATH = (".kew", "private", "prompts")
"""Where the protected artifacts live, relative to the project root."""

RETENTION_DAYS = 30
"""How long a terminal run's exact prompt is retained before purge."""

STORAGE_LOCAL = "local_protected"
STORAGE_UNAVAILABLE = "unavailable"

_DIGEST_PREFIX = "sha256:"
_SUFFIX = ".prompt"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_READ_CHUNK = 65536


class SensitivePromptError(RuntimeError):
    """Base class for every protected-prompt failure."""


class PromptRetentionError(SensitivePromptError):
    """The artifact could not be created or read through a safe boundary."""


class PromptDigestMismatch(SensitivePromptError):
    """The bytes on disk are not the bytes that were retained."""


class PromptUnavailable(SensitivePromptError):
    """The artifact is gone — purged or never retained. Not empty."""


def default_root(project_root: Path | str) -> Path:
    """The protected artifact root for *project_root*."""
    return Path(project_root).joinpath(*PROTECTED_SUBPATH)


def prompt_digest(text: str) -> str:
    """Return ``sha256:<hex>`` over the EXACT UTF-8 bytes of *text*.

    No normalization of any kind: a CRLF prompt and its LF twin are different
    prompts and get different digests.
    """
    return _DIGEST_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_digest(value: object) -> bool:
    """True when *value* is a well-formed ``sha256:<64 hex>`` reference."""
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        return False
    body = value[len(_DIGEST_PREFIX) :]
    return len(body) == 64 and all(char in "0123456789abcdef" for char in body)


def locator_for(run_id: str, attempt_number: int) -> str:
    """The opaque on-disk filename for one attempt's prompt.

    Derived by digest so a hostile ``run_id`` cannot reach outside the root,
    and so the filename discloses neither the run nor the prompt.
    """
    key = f"{run_id}\x00{attempt_number}".encode()
    return hashlib.sha256(key).hexdigest()[:32] + _SUFFIX


def retention_deadline(
    terminal_at: datetime, *, days: int = RETENTION_DAYS
) -> datetime:
    """When a run that went terminal at *terminal_at* may be purged."""
    return terminal_at + timedelta(days=days)


@dataclass(frozen=True)
class RetainedPrompt:
    """A reference to retained bytes — metadata only, never the bytes."""

    run_id: str
    attempt_number: int
    digest: str
    byte_length: int
    locator: str
    created_at: datetime = field(repr=False)

    def artifact_facts(self) -> dict[str, Any]:
        """The digest-only facts an artifact evidence row may carry."""
        return {
            "artifact_kind": PROMPT_ARTIFACT_KIND,
            "digest": self.digest,
            "byte_length": self.byte_length,
            "storage": STORAGE_LOCAL,
        }


def _ensure_root(root: Path) -> None:
    """Create *root* owner-only, refusing a symlinked root outright."""
    if root.is_symlink():
        raise PromptRetentionError(f"protected prompt root is a symlink: {root}")
    try:
        root.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
        # 0o700 TIGHTENS a pre-existing directory and never widens one, so
        # semgrep's "a good default is 0o644" advice is backwards here: the
        # whole point of this root is that nobody but the owner may read it.
        os.chmod(root, _DIR_MODE)  # noqa: S103,E501  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    except OSError as exc:
        raise PromptRetentionError(f"cannot prepare {root}: {exc}") from exc


def _create_exclusive(path: Path, data: bytes) -> None:
    """Create *path* with O_EXCL|O_NOFOLLOW at 0o600 and write *data*."""
    try:
        handle = os.open(path, _CREATE_FLAGS, _FILE_MODE)
    except OSError as exc:
        raise PromptRetentionError(f"refused to create {path.name}: {exc}") from exc
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def _read_bytes(path: Path) -> bytes:
    """Read *path* through an O_NOFOLLOW boundary."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise PromptUnavailable(f"prompt artifact is not retained: {exc}") from exc
    except OSError as exc:
        raise PromptRetentionError(f"refused to read {path.name}: {exc}") from exc
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(handle, _READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(handle)
    return b"".join(chunks)


def retain_prompt(
    root: Path | str,
    *,
    run_id: str,
    attempt_number: int,
    text: str,
    now: datetime,
) -> RetainedPrompt:
    """Retain the exact bytes of *text*, returning a digest-only reference.

    Re-retaining byte-identical text is idempotent. Re-retaining *different*
    text for the same attempt is refused: an attempt's prompt is what it was.
    """
    root = Path(root)
    _ensure_root(root)
    data = text.encode("utf-8")
    ref = RetainedPrompt(
        run_id=run_id,
        attempt_number=attempt_number,
        digest=prompt_digest(text),
        byte_length=len(data),
        locator=locator_for(run_id, attempt_number),
        created_at=now,
    )
    path = root / ref.locator
    if path.exists() and not path.is_symlink():
        if _read_bytes(path) != data:
            raise PromptRetentionError(
                f"a different prompt is already retained for attempt {attempt_number}"
            )
        return ref
    _create_exclusive(path, data)
    return ref


def read_prompt(
    root: Path | str,
    *,
    run_id: str,
    attempt_number: int,
    expect_digest: str,
) -> str:
    """The sensitive-read boundary: return the exact retained bytes.

    Raises :class:`PromptUnavailable` when the artifact was purged or never
    written, :class:`PromptRetentionError` when the path is no longer a plain
    owner-only file, and :class:`PromptDigestMismatch` when the bytes on disk
    are not the bytes that were retained.
    """
    path = Path(root) / locator_for(run_id, attempt_number)
    text = _read_bytes(path).decode("utf-8")
    actual = prompt_digest(text)
    if actual != expect_digest:
        raise PromptDigestMismatch(
            f"retained prompt digest {actual} does not match {expect_digest}"
        )
    return text


def purge_prompt(root: Path | str, *, run_id: str, attempt_number: int) -> bool:
    """Remove the retained bytes. True when something was removed."""
    path = Path(root) / locator_for(run_id, attempt_number)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PromptRetentionError(f"cannot purge {path.name}: {exc}") from exc
    return True


class ExpiringArtifact(Protocol):
    """The metadata a retention sweep needs — structural, not an import."""

    run_id: str
    attempt_number: int
    kind: str


class PromptRetentionStore(Protocol):
    """The normalized-row side of retention (``run_store_artifacts``)."""

    def list_expired_artifacts(
        self, *, now: datetime, kind: str | None = None
    ) -> Sequence[ExpiringArtifact]: ...

    def mark_artifact_purged(
        self,
        *,
        run_id: str,
        attempt_number: int,
        kind: str,
        purged_at: datetime,
        reason: str,
    ) -> bool: ...


def purge_expired_prompts(
    store: PromptRetentionStore,
    root: Path | str,
    *,
    now: datetime,
    reason: str = "retention",
) -> list[str]:
    """Purge every prompt past terminal-plus-30-days and acknowledge each one.

    Bytes already gone from disk are still acknowledged in the metadata: a
    half-purged artifact that keeps claiming to be readable is worse than one
    that reports itself unavailable.
    """
    purged: list[str] = []
    for row in store.list_expired_artifacts(now=now, kind=PROMPT_ARTIFACT_KIND):
        purge_prompt(root, run_id=row.run_id, attempt_number=row.attempt_number)
        store.mark_artifact_purged(
            run_id=row.run_id,
            attempt_number=row.attempt_number,
            kind=PROMPT_ARTIFACT_KIND,
            purged_at=now,
            reason=reason,
        )
        purged.append(row.run_id)
    return purged
