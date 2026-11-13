"""Scan agent output for held credentials before any push (#1969, #1980).

The scan reads every object the push transfers, as raw bytes: each blob, each
tree (file names), and each commit (message, author, committer, and every
other header), enumerated with ``rev-list --objects <head> --not <tips>`` and
replace objects disabled. It matches the values of the
credentials the publishing process holds, in raw form and in the encodings an
agent could produce with one command (standard and URL-safe base64 at every
byte alignment, hex in either case, URL percent-encoding), plus the key shapes
kew already recognises. Values shorter than :data:`MIN_VALUE_CHARS` are not
scanned: a short value matches ordinary text. The needle set is exact: the
credential names runners declare, the names kew holds for publication and
key issuance, and the secret fields of the host's Codex CLI login file, never a
name heuristic.

A held credential value counts wherever it appears. A key-shape match in a
file counts unless that exact string is already in the trusted base commit's
version of every path the file is pushed at: a placeholder the repository
already carries is not agent output. A path new at the base, a commit message
or a file name always counts.

Reads are bounded like the canonical reader: per object, per push, and in
time; over a bound the scan refuses rather than publish unscanned. Key-shape
strings are held once for the whole push, settled against the base for each
blob holding them, under the same deadline; more than
:data:`MAX_KEY_SHAPE_MATCHES` distinct strings across the push refuse as a
finding, since kew cannot clear that many. Repeating one string in any number
of blobs is not over that bound, and costs neither a copy of its bytes nor a
search of base content already answered for it. What repeats do bound is
:data:`MAX_KEY_SHAPE_SETTLEMENTS`, the ``(blob, string)`` pairs held at all.

A head the agent already pushed (``already_published``) is scanned over its
whole range from the base: a range cut by a shallow boundary, or a head that
does not descend from the base, refuses as unavailable rather than scan only
its top.

The result names counts, credential names, key-shape classes and object types.
No value, fragment, path or matching line is ever returned, recorded or
printed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess  # nosec B404 — only for TimeoutExpired from run_bytes
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal
from urllib.parse import quote, quote_plus

from kew import credential_redaction
from kew.credential_issuer import PROVISIONING_ENV
from kew.credential_surface import _OPENROUTER_KEY_RE, _issued_secrets
from kew.publication import DETACHED_PUBLICATION_TOKEN_ENV
from kew.subprocess_utils import popen_bytes, run_bytes

if TYPE_CHECKING:
    from kew.run_store import RunStore

__all__ = [
    "ALREADY_PUBLISHED_EVENT",
    "KEY_SHAPES",
    "KEY_SHAPE_OVERFLOW",
    "KEY_SHAPE_SETTLE_OVERFLOW",
    "MAX_KEY_SHAPE_MATCHES",
    "MAX_KEY_SHAPE_SETTLEMENTS",
    "MIN_VALUE_CHARS",
    "OUTPUT_SCAN_EVENT",
    "OutputScan",
    "OutputScanError",
    "OutputScanOversize",
    "OutputScanTarget",
    "OutputScanTimeout",
    "PUBLICATION_CREDENTIAL_NAMES",
    "ScanFinding",
    "codex_auth_values",
    "output_scan_names",
    "output_scan_values",
    "range_truncated",
    "record_already_published",
    "record_output_scan",
    "recorded_scan_verdict",
    "scan_output",
]

OUTPUT_SCAN_EVENT = "publication_output_scan"
ALREADY_PUBLISHED_EVENT = "credential_already_published"

#: Shortest credential value the scan matches. Shorter values collide with
#: ordinary identifiers and prose; every provider key kew handles is longer.
MIN_VALUE_CHARS = 16

#: Key shapes matched whatever the process holds. Bounded bodies keep
#: documentation placeholders such as ``sk-ant-...`` out of the match.
KEY_SHAPES: Mapping[str, re.Pattern[str]] = {
    "anthropic_key_shape": re.compile(r"sk-ant-[A-Za-z0-9_-]{32,}"),
    "openrouter_key_shape": _OPENROUTER_KEY_RE,
    "github_token_shape": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"
    ),
    # Project, service-account and admin keys carry a typed prefix; a legacy
    # key is ``sk-`` straight into its body. Both need 40+ key characters, so
    # ``sk-learn`` never matches, and ``sk-ant-``/``sk-or-v1-`` (a hyphen
    # after the class word) never match the legacy branch.
    "openai_key_shape": re.compile(
        r"\bsk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}|[A-Za-z0-9]{40,})"
    ),
}

_OID = re.compile(r"[0-9a-f]{40}")

#: Most distinct key-shape strings the scan checks against the base, however
#: many blobs hold each one. Every held string is settled against every path
#: its blobs are pushed at, under the scan's deadline; over this many distinct
#: strings the scan cannot clear them and refuses (``KEY_SHAPE_OVERFLOW``).
MAX_KEY_SHAPE_MATCHES = 256
KEY_SHAPE_OVERFLOW = "key_shape_overflow"

#: Most ``(blob, key-shape string)`` pairs the scan holds to settle, which is
#: what the settling costs and what it keeps in memory: the distinct bound
#: above says nothing about how many blobs repeat each string. Every held
#: string in as many blobs as that bound allows distinct strings, so a
#: placeholder the base carries in thousands of files is well within it; over
#: this many pairs the scan refuses (``KEY_SHAPE_SETTLE_OVERFLOW``) rather
#: than hold the push in memory or settle past its deadline.
MAX_KEY_SHAPE_SETTLEMENTS = 65536
KEY_SHAPE_SETTLE_OVERFLOW = "key_shape_settle_overflow"


class OutputScanError(RuntimeError):
    """The scan could not read what it was asked to scan."""

    reason = "credential_scan_unavailable"


class OutputScanOversize(OutputScanError):
    """An object, or the whole push, exceeds the scan's byte bounds."""

    reason = "credential_scan_oversize"


class OutputScanTimeout(OutputScanError):
    """Git did not answer the scan within its deadline."""

    reason = "credential_scan_timeout"


@dataclass(frozen=True)
class OutputScanTarget:
    """One publication: the commit about to be pushed and what it builds on.

    ``base_sha`` and ``other_parents`` are the tips the remote already has:
    objects reachable from them are not sent, so they are not scanned.
    ``env`` must be the hardened environment of the push that follows.
    ``already_published`` says the remote already holds ``head_sha`` because
    the agent pushed it itself; it is set per target, never per guard.
    ``objects``, when given, is the exact object set that will be sent, and is
    what the scan reads instead of the push's own listing: a transfer that is
    not a push (a remote host's bundle) names what it carries itself.
    """

    repository: Path
    base_sha: str
    head_sha: str
    other_parents: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    already_published: bool = False
    objects: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScanFinding:
    """Where a match was, by name: never a value, fragment, path or line.

    ``source`` is the credential NAME, ``issued_run_key``, or a key-shape class;
    ``encoding`` is the value class (``value_raw`` ...) or ``key_shape``.
    """

    source: str
    object_type: str
    encoding: str
    count: int

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "object": self.object_type,
            "encoding": self.encoding,
            "count": self.count,
        }

    def describe(self) -> str:
        if self.source == KEY_SHAPE_OVERFLOW:
            return (
                f"more than {MAX_KEY_SHAPE_MATCHES} distinct key-shape strings in"
                " blobs; kew cannot clear that many against the base"
            )
        if self.source == KEY_SHAPE_SETTLE_OVERFLOW:
            return (
                f"more than {MAX_KEY_SHAPE_SETTLEMENTS} (file, key-shape string)"
                " pairs in blobs; kew cannot settle that many against the base"
            )
        return f"{self.source} in {self.object_type} ({self.encoding})"


@dataclass(frozen=True)
class OutputScan:
    """Counts and names only: never a value, a fragment, a path or a line."""

    verdict: Literal["clean", "found"]
    files_scanned: int
    matches: Mapping[str, int]
    findings: tuple[ScanFinding, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "files_scanned": self.files_scanned,
            "matches": dict(self.matches),
            "findings": [finding.payload() for finding in self.findings],
        }

    def describe(self) -> str:
        """The refusal's diagnosis: names, object types and classes only."""
        return "; ".join(finding.describe() for finding in self.findings)


@dataclass(frozen=True)
class _Needle:
    source: str
    label: str
    pattern: re.Pattern[bytes]


#: Credential names kew itself holds for publication and key issuance.
PUBLICATION_CREDENTIAL_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    DETACHED_PUBLICATION_TOKEN_ENV,
    PROVISIONING_ENV,
)
ISSUED_RUN_KEY = "issued_run_key"

#: The official Codex CLI login file, and the fields in it that hold a secret.
#: A native Codex run can read this file (docs/security.md), so its values are
#: needles like any held credential. ``account_id`` and ``last_refresh`` are
#: identifiers, not secrets.
CODEX_AUTH_SOURCE = "codex_auth"
_CODEX_AUTH_FIELDS: tuple[tuple[str, ...], ...] = (
    ("OPENAI_API_KEY",),
    ("tokens", "access_token"),
    ("tokens", "refresh_token"),
    ("tokens", "id_token"),
)
#: Largest login file the scan reads; a real one is a few kilobytes.
_MAX_CODEX_AUTH_BYTES = 1024 * 1024


def output_scan_names() -> tuple[str, ...]:
    """The exact credential names whose values the gate matches.

    Every runner's declared credential names, plus the publication and
    provisioning names kew holds. Never a name heuristic: a gate must not
    refuse on a variable that only looks secret (``GIT_AUTHOR_NAME``).
    """
    from kew.runners.registry import KNOWN_RUNNER_NAMES, runner_required_credentials

    declared = {
        name
        for runner in KNOWN_RUNNER_NAMES
        for name in runner_required_credentials(runner)
    }
    return tuple(sorted(declared | set(PUBLICATION_CREDENTIAL_NAMES)))


def output_scan_values(
    environ: Mapping[str, str] | None = None,
    *,
    admitted_names: Iterable[str] = (),
    codex_login_required: bool = False,
) -> tuple[tuple[str, str], ...]:
    """``(name, value)`` for every held credential at or above the bound.

    The environment's declared names, the ``env:`` names the publishing run
    was admitted with (``admitted_names``), issued run keys, and the host
    Codex login file's secret fields (:func:`codex_auth_values`, strict when
    ``codex_login_required``).
    """
    source = os.environ if environ is None else environ
    names = {*output_scan_names(), *admitted_names}
    pairs = {(name, source.get(name, "")) for name in names}
    pairs.update((ISSUED_RUN_KEY, secret) for secret in _issued_secrets())
    pairs.update(codex_auth_values(source, required=codex_login_required))
    return tuple(
        sorted(
            (pair for pair in pairs if len(pair[1]) >= MIN_VALUE_CHARS),
            key=lambda pair: (-len(pair[1]), pair),
        )
    )


def _codex_auth_path(source: Mapping[str, str]) -> Path | None:
    """``$CODEX_HOME/auth.json``, else ``$HOME/.codex/auth.json``, from *source*."""
    codex_home = source.get("CODEX_HOME", "")
    if codex_home:
        return Path(codex_home) / "auth.json"
    home = source.get("HOME", "")
    return Path(home) / ".codex" / "auth.json" if home else None


def _read_codex_auth(path: Path) -> bytes:
    """The login file's bytes: a regular file under the bound, never a FIFO.

    Opened non-blocking and checked with ``fstat``, so a FIFO or a device put
    where the file belongs cannot stall the scan.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("the Codex login path is not a regular file")
        if info.st_size > _MAX_CODEX_AUTH_BYTES:
            raise OSError("the Codex login file exceeds its bound")
        return handle.read(_MAX_CODEX_AUTH_BYTES + 1)


def codex_auth_values(
    source: Mapping[str, str], *, required: bool = False
) -> tuple[tuple[str, str], ...]:
    """``(name, value)`` for each secret field of the host Codex login file.

    Collected like the environment values: an absent file adds no needle, as
    an unset variable does. An unreadable or malformed file adds none either,
    and the log says so. Names are ``codex_auth:<field>``.

    ``required`` is for a run admitted under the native Codex posture, which
    can read the login file. There a login the scan cannot hold (absent,
    unreadable, malformed, oversize, or not a regular file) raises
    :class:`OutputScanError`: the scan could not prove the login's tokens are
    absent from the output.
    """
    document = _codex_auth_document(source, required=required)
    return () if document is None else _codex_secret_fields(document)


def _codex_auth_document(source: Mapping[str, str], *, required: bool) -> object:
    """The parsed login file, or ``None`` when it adds no needle (lenient only)."""
    path = _codex_auth_path(source)
    if path is None or not path.exists():
        if required:
            raise OutputScanError("the host Codex login file is absent")
        return None
    try:
        return json.loads(_read_codex_auth(path))
    except (OSError, ValueError) as exc:
        if required:
            raise OutputScanError(
                f"the host Codex login file was not read ({type(exc).__name__})"
            ) from None
        logging.getLogger(__name__).warning(
            "Codex login file %s was not read for the publication scan (%s)",
            path,
            type(exc).__name__,
        )
        return None


def _codex_secret_fields(document: object) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for field in _CODEX_AUTH_FIELDS:
        value: object = document
        for key in field:
            value = value.get(key) if isinstance(value, Mapping) else None
        if isinstance(value, str) and value:
            pairs.append((f"{CODEX_AUTH_SOURCE}:{'.'.join(field)}", value))
    return tuple(pairs)


def _as_bytes(pattern: re.Pattern[str]) -> re.Pattern[bytes]:
    return re.compile(pattern.pattern.encode("utf-8"), pattern.flags & re.IGNORECASE)


def _value_needles(name: str, value: str) -> list[_Needle]:
    form = credential_redaction.make_form(value, short=False)
    blocks = [_as_bytes(block) for block in form.blocks]
    needles = [
        _Needle(name, "value_raw", re.compile(re.escape(value.encode()))),
        _Needle(name, "value_hex", blocks[0]),
        *(_Needle(name, "value_base64", block) for block in blocks[1:4]),
        *(_Needle(name, "value_base64url", block) for block in blocks[4:7]),
    ]
    for encoded in {quote(value, safe=""), quote_plus(value, safe="")} - {value}:
        needles.append(
            _Needle(name, "value_percent", re.compile(re.escape(encoded.encode())))
        )
    return needles


def _needles(values: Iterable[tuple[str, str]]) -> list[list[_Needle]]:
    return [
        _value_needles(name, value)
        for name, value in values
        if len(value) >= MIN_VALUE_CHARS
    ]


_SHAPE_NEEDLES = tuple(
    _Needle(label, label, _as_bytes(pattern)) for label, pattern in KEY_SHAPES.items()
)


class _Tally:
    """Match counts per class and per (source, object type, encoding)."""

    def __init__(self) -> None:
        self.matches: dict[str, int] = {}
        self.findings: dict[tuple[str, str, str], int] = {}
        #: Each distinct key-shape string held until the base is consulted,
        #: with the shape it matched and every blob holding it. Keyed by the
        #: string, so the bytes are kept once however many blobs repeat it and
        #: the push is never held in memory. At most
        #: :data:`MAX_KEY_SHAPE_MATCHES` strings and
        #: :data:`MAX_KEY_SHAPE_SETTLEMENTS` ``(blob, string)`` pairs.
        self.shapes: dict[bytes, tuple[_Needle, set[str]]] = {}
        self.pairs = 0
        self.overflow: str | None = None

    def add(self, object_type: str, needle: _Needle, encoding: str) -> None:
        label = f"{_TYPE_PREFIX[object_type]}{needle.label}"
        self.matches[label] = self.matches.get(label, 0) + 1
        key = (needle.source, object_type, encoding)
        self.findings[key] = self.findings.get(key, 0) + 1

    def scan(
        self, oid: str, object_type: str, raw: bytes, values: list[list[_Needle]]
    ) -> None:
        for needles in values:
            seen: set[tuple[int, int]] = set()
            for needle in needles:
                for match in needle.pattern.finditer(raw):
                    if match.span() not in seen:
                        seen.add(match.span())
                        self.add(object_type, needle, needle.label)
        for needle in _SHAPE_NEEDLES:
            for match in needle.pattern.finditer(raw):
                if object_type != "blob":
                    self.add(object_type, needle, "key_shape")
                elif not self._retain(oid, needle, match.group()):
                    break

    def _retain(self, oid: str, needle: _Needle, text: bytes) -> bool:
        """Hold one blob key-shape string once; False once over a bound.

        Two bounds, because they count different things. A string already held
        for another blob is held again for this one -- the base must carry it
        at every path each blob is pushed at -- so the distinct strings bound
        what has to be cleared, and the ``(blob, string)`` pairs bound the
        settling itself: how much is held, and how many lookups it takes.
        """
        if self.overflow is not None:
            return False
        entry = self.shapes.get(text)
        if entry is None:
            if len(self.shapes) >= MAX_KEY_SHAPE_MATCHES:
                return self._refuse(KEY_SHAPE_OVERFLOW)
            entry = self.shapes[text] = (needle, set())
        if oid in entry[1]:
            return True
        if self.pairs >= MAX_KEY_SHAPE_SETTLEMENTS:
            return self._refuse(KEY_SHAPE_SETTLE_OVERFLOW)
        entry[1].add(oid)
        self.pairs += 1
        return True

    def _refuse(self, reason: str) -> bool:
        """Refuse whatever the base holds: drop what is held, check none."""
        self.overflow = reason
        self.shapes = {}
        return False

    def shape_blobs(self) -> set[str]:
        """Every blob oid holding a key-shape string still to be settled."""
        return {oid for _, holders in self.shapes.values() for oid in holders}

    def settle_shapes(
        self, carried: Callable[[str, bytes], bool], deadline: float
    ) -> None:
        """Count each held blob key-shape string the base does not already carry."""
        for text, (needle, holders) in self.shapes.items():
            for oid in holders:
                if time.monotonic() >= deadline:
                    raise OutputScanTimeout("publication scan exceeded its deadline")
                if not carried(oid, text):
                    self.add("blob", needle, "key_shape")
        self.shapes = {}

    def result(self, files: int) -> OutputScan:
        if self.overflow is not None:
            self.matches[self.overflow] = 1
            self.findings[(self.overflow, "blob", "key_shape")] = 1
        findings = tuple(
            ScanFinding(source, object_type, encoding, count)
            for (source, object_type, encoding), count in sorted(self.findings.items())
        )
        return OutputScan(
            verdict="found" if findings else "clean",
            files_scanned=files,
            matches=dict(sorted(self.matches.items())),
            findings=findings,
        )


#: Evidence class prefix per object type. Blobs keep the bare classes; tree
#: entries carry file names, commits carry messages and identities.
_TYPE_PREFIX = {"blob": "", "tree": "tree_", "commit": "commit_", "tag": "tag_"}


def _scan_bounds() -> tuple[int, int, float]:
    """The canonical reader's bounds: per object, per push, and time."""
    from kew.publication_artifact import MAX_BLOB_BYTES, MAX_TOTAL_BYTES
    from kew.publication_canonical import BASE_SCAN_TIMEOUT_SECONDS

    return MAX_BLOB_BYTES, MAX_TOTAL_BYTES, float(BASE_SCAN_TIMEOUT_SECONDS)


def _git_command(target: OutputScanTarget, *args: str) -> list[str]:
    return ["git", "--no-replace-objects", "-C", str(target.repository), *args]


def _git_env(target: OutputScanTarget) -> dict[str, str] | None:
    return None if target.env is None else dict(target.env)


def _git_bytes(
    target: OutputScanTarget,
    *args: str,
    deadline: float,
    stdin: bytes | None = None,
) -> bytes:
    """Run git with replace objects off, within the scan's deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OutputScanTimeout("publication scan exceeded its deadline")
    try:
        result = run_bytes(
            _git_command(target, *args),
            stdin=stdin,
            env=_git_env(target),
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise OutputScanTimeout(f"git {args[0]} timed out while scanning") from exc
    if result.returncode != 0:
        raise OutputScanError(f"git {args[0]} failed while scanning publication")
    return result.stdout or b""


def _pushed_objects(target: OutputScanTarget, deadline: float) -> list[str]:
    """Every object the push transfers: reachable from the head, not the tips.

    The object set, not a diff view: a blob written in one commit and removed
    in the next is still sent, so it is still scanned.
    """
    for sha in (target.base_sha, target.head_sha, *target.other_parents):
        if not _OID.fullmatch(sha):
            raise OutputScanError("publication scan needs full commit ids")
    listing = _git_bytes(
        target,
        "rev-list",
        "--objects",
        "--no-object-names",
        target.head_sha,
        "--not",
        target.base_sha,
        *target.other_parents,
        deadline=deadline,
    )
    oids = listing.decode("ascii", errors="strict").split()
    if not all(_OID.fullmatch(oid) for oid in oids):
        raise OutputScanError("publication object listing is malformed")
    return oids


def _scanned_objects(target: OutputScanTarget, deadline: float) -> list[str]:
    """The target's own object set when it names one, else the push's."""
    if target.objects is None:
        return _pushed_objects(target, deadline)
    oids = list(dict.fromkeys(target.objects))
    if not all(_OID.fullmatch(oid) for oid in oids):
        raise OutputScanError("publication scan needs full object ids")
    return oids


def _shallow_commits(target: OutputScanTarget, deadline: float) -> frozenset[str]:
    """The commits whose parents the scanned repository was never given."""
    listed = _git_bytes(target, "rev-parse", "--git-path", "shallow", deadline=deadline)
    path = Path(listed.decode("utf-8", errors="strict").strip())
    if not path.is_absolute():
        path = target.repository / path
    if not path.exists():
        # A complete repository has no shallow list; nothing is cut short.
        return frozenset()
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise OutputScanError(
            "the scanned repository's shallow list is unreadable"
        ) from exc
    commits = frozenset(text.split())
    if not all(_OID.fullmatch(oid) for oid in commits):
        raise OutputScanError("the scanned repository's shallow list is malformed")
    return commits


def range_truncated(target: OutputScanTarget, deadline: float) -> bool:
    """True when a commit the scan would read has parents the repository lacks.

    ``rev-list`` stops at a shallow boundary without an error, so a range cut
    there lists only its top: earlier commits, and blobs a later commit
    removed, would never be read.
    """
    shallow = _shallow_commits(target, deadline)
    return bool(shallow) and not shallow.isdisjoint(_pushed_objects(target, deadline))


def _require_complete_range(
    target: OutputScanTarget, oids: list[str], deadline: float
) -> None:
    """An already-published range is scanned whole, from the base, or not at all.

    The agent pushed every commit between ``base_sha`` and ``head_sha`` itself,
    so each one is agent output. A range truncated by a shallow boundary, or a
    head that does not descend from the base, cannot be proven scanned.
    """
    if not _shallow_commits(target, deadline).isdisjoint(oids):
        raise OutputScanError("the pushed range is truncated by a shallow boundary")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OutputScanTimeout("publication scan exceeded its deadline")
    try:
        ancestry = run_bytes(
            _git_command(
                target, "merge-base", "--is-ancestor", target.base_sha, target.head_sha
            ),
            env=_git_env(target),
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise OutputScanTimeout("git merge-base timed out while scanning") from exc
    if ancestry.returncode != 0:
        raise OutputScanError("the pushed head does not descend from the base")


def _size_row(oid: str, line: bytes) -> tuple[str, str, int]:
    fields = line.decode("ascii", errors="strict").split()
    if len(fields) != 3 or fields[0] != oid or not fields[2].isdigit():
        raise OutputScanError("publication object sizes are malformed")
    if fields[1] not in _TYPE_PREFIX:
        raise OutputScanError("publication object has an unknown type")
    return oid, fields[1], int(fields[2])


def _checked_sizes(
    target: OutputScanTarget, oids: list[str], deadline: float
) -> list[tuple[str, str, int]]:
    """``(oid, type, size)`` for each object; oversize refuses before any read."""
    max_object, max_total, _ = _scan_bounds()
    listing = _git_bytes(
        target,
        "cat-file",
        "--batch-check",
        stdin="".join(f"{oid}\n" for oid in oids).encode(),
        deadline=deadline,
    )
    lines = listing.splitlines()
    if len(lines) != len(oids):
        raise OutputScanError("publication object sizes are incomplete")
    rows = [_size_row(oid, line) for oid, line in zip(oids, lines, strict=True)]
    if any(size > max_object for _, _, size in rows):
        raise OutputScanOversize(f"a pushed object exceeds {max_object} bytes")
    if sum(size for _, _, size in rows) > max_total:
        raise OutputScanOversize(f"the pushed objects exceed {max_total} bytes")
    return rows


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(min(1024 * 1024, size - len(chunks)))
        if not chunk:
            raise OutputScanError("publication object batch ended early")
        chunks.extend(chunk)
    return bytes(chunks)


def _stream_objects(
    target: OutputScanTarget,
    rows: list[tuple[str, str, int]],
    deadline: float,
    visit: Callable[[str, str, bytes], None],
) -> None:
    """Read one object at a time under a watchdog; never hold the whole push."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OutputScanTimeout("publication scan exceeded its deadline")
    process = popen_bytes(
        _git_command(target, "cat-file", "--batch"), env=_git_env(target)
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    fired = threading.Event()

    def expire() -> None:
        fired.set()
        process.kill()

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    try:
        _feed_batch(process, rows, visit)
        process.stdin.close()
        if process.wait() != 0:
            raise OutputScanError("git cat-file failed while scanning publication")
    except (OutputScanError, OSError, ValueError) as exc:
        if fired.is_set():
            raise OutputScanTimeout("git cat-file timed out while scanning") from exc
        raise
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _feed_batch(
    process: Any,
    rows: list[tuple[str, str, int]],
    visit: Callable[[str, str, bytes], None],
) -> None:
    for oid, object_type, size in rows:
        process.stdin.write(f"{oid}\n".encode())
        process.stdin.flush()
        header = process.stdout.readline().decode("ascii", errors="strict").split()
        if header != [oid, object_type, str(size)]:
            raise OutputScanError("publication object batch is malformed")
        raw = _read_exact(process.stdout, size)
        if process.stdout.read(1) != b"\n":
            raise OutputScanError("publication object batch is truncated")
        visit(oid, object_type, raw)


def scan_output(
    target: OutputScanTarget, values: Iterable[tuple[str, str]]
) -> OutputScan:
    """Scan every object the push transfers, as raw bytes, within bounds.

    Blobs count under the bare classes, tree objects (file names) under
    ``tree_``, and commit objects (message, author, committer, encoding,
    signature, every header) under ``commit_``. Each finding names its source
    credential or key shape and its object type.
    """
    _, _, seconds = _scan_bounds()
    deadline = time.monotonic() + seconds
    needles = _needles(values)
    oids = _scanned_objects(target, deadline)
    if target.already_published:
        _require_complete_range(target, oids, deadline)
    rows = _checked_sizes(target, oids, deadline) if oids else []
    tally = _Tally()
    if rows:
        _stream_objects(
            target,
            rows,
            deadline,
            lambda oid, object_type, raw: tally.scan(oid, object_type, raw, needles),
        )
    if tally.shapes:
        commits = [oid for oid, kind, _ in rows if kind == "commit"]
        tally.settle_shapes(
            _base_carries(target, commits, tally.shape_blobs(), deadline), deadline
        )
    return tally.result(files=sum(1 for _, kind, _ in rows if kind == "blob"))


def _tree_blobs(
    target: OutputScanTarget, commit: str, deadline: float
) -> list[tuple[str, bytes]]:
    """``(blob oid, path)`` for every file in *commit*'s tree, paths as bytes."""
    listing = _git_bytes(
        target, "ls-tree", "-r", "-z", "--full-tree", commit, deadline=deadline
    )
    entries: list[tuple[str, bytes]] = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, tab, path = record.partition(b"\t")
        fields = meta.decode("ascii", errors="strict").split()
        if not tab or len(fields) != 3 or not _OID.fullmatch(fields[2]):
            raise OutputScanError("publication tree listing is malformed")
        if fields[1] == "blob":
            entries.append((fields[2], path))
    return entries


def _read_blobs(
    target: OutputScanTarget, oids: list[str], deadline: float
) -> dict[str, bytes]:
    """Read base blobs under the push's per-object and total bounds."""
    blobs: dict[str, bytes] = {}
    if not oids:
        return blobs
    rows = _checked_sizes(target, oids, deadline)

    def keep(oid: str, object_type: str, raw: bytes) -> None:
        if object_type != "blob":
            raise OutputScanError("a base path is not a blob")
        blobs[oid] = raw

    _stream_objects(target, rows, deadline, keep)
    return blobs


def _base_carries(
    target: OutputScanTarget,
    commits: list[str],
    held: set[str],
    deadline: float,
) -> Callable[[str, bytes], bool]:
    """Whether a pushed blob's key-shape string is already at the trusted base.

    True only when every path the blob is pushed at exists in the base commit
    and that path's base content holds the exact string. A blob with no known
    path, or any path new at the base, is not carried: its match counts.

    Each blob's paths collapse to the distinct base blobs they name, and every
    answer is kept, so settling a string costs one search per base blob that
    has to answer for it rather than one per ``(blob, string, path)``: the work
    is the base content read times the distinct strings held, whatever the push
    repeats.
    """
    paths: dict[str, set[bytes]] = {}
    for commit in commits:
        for oid, path in _tree_blobs(target, commit, deadline):
            if oid in held:
                paths.setdefault(oid, set()).add(path)
    base = {path: oid for oid, path in _tree_blobs(target, target.base_sha, deadline)}
    wanted = {base[path] for where in paths.values() for path in where if path in base}
    blobs = _read_blobs(target, sorted(wanted), deadline)
    sources = _base_sources(paths, base)
    holds: dict[tuple[str, bytes], bool] = {}
    cleared: dict[tuple[frozenset[str], bytes], bool] = {}

    def holds_text(base_oid: str, text: bytes) -> bool:
        answer = holds.get((base_oid, text))
        if answer is None:
            answer = holds[(base_oid, text)] = text in blobs[base_oid]
        return answer

    def carried(oid: str, text: bytes) -> bool:
        source = sources.get(oid)
        if source is None:
            return False
        answer = cleared.get((source, text))
        if answer is None:
            answer = cleared[(source, text)] = all(
                holds_text(base_oid, text) for base_oid in source
            )
        return answer

    return carried


def _base_sources(
    paths: dict[str, set[bytes]], base: dict[bytes, str]
) -> dict[str, frozenset[str] | None]:
    """The base blobs each pushed blob's paths name, or ``None`` for no answer.

    ``None`` when the blob has no known path or any path is new at the base: a
    string in it counts whatever the base holds elsewhere. Blobs naming the
    same base blobs share one set, so they share one settled answer too.
    """
    pool: dict[frozenset[str], frozenset[str]] = {}
    sources: dict[str, frozenset[str] | None] = {}
    for oid, where in paths.items():
        if not where or not all(path in base for path in where):
            sources[oid] = None
            continue
        source = frozenset(base[path] for path in where)
        sources[oid] = pool.setdefault(source, source)
    return sources


def record_output_scan(
    store: RunStore,
    *,
    run_id: str,
    attempt_number: int,
    item_id: str | None,
    target: OutputScanTarget,
    result: OutputScan,
    already_published: bool = False,
) -> None:
    """Record the verdict bound to the run, attempt and exact head, strictly.

    ``already_published`` says the agent itself pushed the scanned commits
    before the host scan, so a finding is already on the remote branch.
    """
    from kew.audit import Actor, emit

    emit(
        store,
        OUTPUT_SCAN_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        item_id=item_id,
        payload={
            "attempt_number": attempt_number,
            "base_sha": target.base_sha,
            "head_sha": target.head_sha,
            "min_value_chars": MIN_VALUE_CHARS,
            "already_published": already_published,
            **result.payload(),
        },
        strict=True,
    )


def record_already_published(
    store: RunStore,
    *,
    run_id: str,
    attempt_number: int,
    item_id: str | None,
    target: OutputScanTarget,
    result: OutputScan,
) -> None:
    """Record that a held credential already reached the remote branch.

    Names and object types only, like the scan record. Removing the commit and
    rotating the credential are the operator's actions.
    """
    from kew.audit import Actor, emit

    emit(
        store,
        ALREADY_PUBLISHED_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        item_id=item_id,
        payload={
            "attempt_number": attempt_number,
            "base_sha": target.base_sha,
            "head_sha": target.head_sha,
            "findings": [finding.payload() for finding in result.findings],
            "operator_action": "rotate the credential; remove the commit",
        },
        strict=True,
    )


def recorded_scan_verdict(
    store: RunStore, *, run_id: str, head_sha: str
) -> Literal["clean", "found", "missing"]:
    """The host-recorded verdict for exactly this run and head.

    Every record of the head counts, however many scans of other heads the
    run recorded since. A recorded ``found`` is final: it wins over any clean
    record of the head, earlier or later.
    """
    verdicts = store.head_audit_verdicts(
        event=OUTPUT_SCAN_EVENT,
        run_id=run_id,
        actor=("policy", "host-publication"),
        head_sha=head_sha,
    )
    if "found" in verdicts:
        return "found"
    return "clean" if "clean" in verdicts else "missing"
