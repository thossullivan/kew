"""Bringing the agent's work back off the gateway (epic ``openshell-runtime``,
node N7).

On docker the agent writes into a bind-mounted host directory, so the tree kew
publishes IS the tree the agent edited. On this runtime the checkout was
UPLOADED (``openshell_launch.CHECKOUT_PATH``) and the agent's edits live inside
the sandbox, so the host copy is a snapshot of the launch and publishing it
would publish nothing the agent did. This module is the step that closes that
gap: download the sandbox's checkout back to the host, prove the download is
publishable, and keep what the gateway denied the agent while the sandbox still
exists to be asked.

Four decisions shape it.

**The download lands in a NEW, EMPTY directory, never over the original.** A
download into an existing tree merges: every file the agent DELETED is still
there afterwards, so the deletion silently never reaches the PR. The realized
checkout is therefore a sibling of the original
(:func:`realized_checkout_dir`), refused if anything already occupies it, and
the original is left exactly as the launch uploaded it — which is also what
makes a failed realization safe to refuse rather than publish. The one tree
that occupies it legitimately is an earlier RETRY ATTEMPT's, because the retry
ladder re-enters the same host checkout; a launch retires that one
(:func:`retire_realized_checkout`) so the refusal above keeps meaning
"something kew did not put here".

**The caps are enforced on arrival, and the room for them before it.** The
checkout quota (``kew.sandbox.CHECKOUT_QUOTA_BYTES`` /
``CHECKOUT_QUOTA_ENTRIES``) is enforced on docker by the stream guard walking
the bind mount; there is no host tree to walk while an OpenShell agent runs,
so the one moment the figure becomes knowable is after the transfer. A breach
refuses publication and is recorded — the agent that filled its sandbox does
not get to fill the host and then land a PR made of it. Because the transfer
is the gateway CLI's and kew cannot stop it halfway, two things bracket it: the
host must have room for a cap-sized checkout BEFORE it starts
(:func:`_require_host_room`), and a refused download is REMOVED
(:func:`_discard_refused_download`) — a tree kew will not publish may not keep
the host's free space either.

**``.git`` and the file modes are verified, not assumed.** A transfer that
dropped ``.git`` produced something that cannot be published at all, so that is
a refusal. Modes are softer: a transfer that normalizes them loses the index's
mode information, which is recoverable from the index itself — so it is
repaired and RECORDED rather than refused. The repair only fires on the
signature of a flattened transfer: SEVERAL indexed executables all came back
non-executable, or several indexed plain files all came back executable. One
file losing its bit among others that kept theirs is the agent's own
``chmod -x`` and must survive — and so does the one on a side of the index
that holds nothing else, because there the two readings are the same
observation and the agent's edit is the one kew does not get to undo.

**The denial events are read before the delete.** ``openshell logs`` is the
only account of what the gateway refused the agent, and the sandbox that holds
it is deleted moments later. So the log is read first, on every path, and the
lines that read as denials are kept in the run's evidence. On the paths that
end the workload BY deleting it — a cap breach, a launch unwind, an
interruption — the read is the delete's own
(``OpenShellSandboxHandle._preserve_denials``) and the realization it reaches
afterwards carries it (:func:`released_realization`).
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess  # nosec B404 — only for TimeoutExpired from run_bytes
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kew.credential_surface import redact_credentials
from kew.openshell import OpenShellClient, OpenShellError
from kew.openshell_launch import CHECKOUT_PATH
from kew.publication_apply import trusted_git_env
from kew.sandbox import (
    CHECKOUT_QUOTA_BYTES,
    CHECKOUT_QUOTA_ENTRIES,
    HOST_FREE_SPACE_FLOOR_BYTES,
    checkout_usage,
    remove_prompt_handoff,
)

logger = logging.getLogger(__name__)

#: Suffix of the host directory the realized checkout lands in, beside the
#: original. A sibling rather than a path inside it: a directory under the
#: uploaded checkout would be part of the next thing that walks it, and the
#: whole point is that the two trees stay separate.
REALIZED_SUFFIX = "-realized"

#: The two index modes this module restores. ``120000`` (symlink) and
#: ``160000`` (gitlink) have no mode bits to restore, and either of these is
#: restored only alongside the other, in the cases where the whole transfer is
#: proven to have dropped modes (:func:`_modes_were_flattened`).
EXECUTABLE_INDEX_MODE = "100755"
PLAIN_INDEX_MODE = "100644"
_RESTORABLE_INDEX_MODES = frozenset({EXECUTABLE_INDEX_MODE, PLAIN_INDEX_MODE})

#: How many indexed files one side of the index has to hold before "every one
#: of them contradicts the index" reads as a flattened transfer. A side holding
#: ONE file cannot tell the two apart: "the only indexed executable came back
#: non-executable" is exactly what a normalizing transfer produces AND exactly
#: what the agent's own ``chmod -x`` on it produces, with nothing else on that
#: side left to agree with the index. The tie goes to the agent's edit (PR
#: #2451 review) — the cost is one file's bit arriving as the transfer left it,
#: against a repair that silently reverts work the agent did.
_MIN_FLATTEN_EVIDENCE = 2

#: The executable bits, and the read bits they are derived from. Restoring
#: ``100755`` adds execute wherever read is already permitted, which is what
#: git itself does for a ``chmod +x`` — rather than a flat ``0o755`` that would
#: widen a deliberately private file.
_EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_READ_BITS = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH

#: How long the index read is given. Local plumbing on a tree that is already
#: on this disk; a longer wait would only hold a finished run open. An overrun
#: refuses this realization (:data:`REFUSAL_INDEX_UNREADABLE`) rather than
#: escaping the realization's typed handler.
_INDEX_READ_TIMEOUT_SECONDS = 60.0

#: Substrings that mark a sandbox log line as the gateway refusing the agent
#: something. Deliberately a conservative SUPERSET: v0.1.2 publishes no
#: machine-readable denial schema kew's recorded fixtures could pin, and the
#: sandbox holding the log is deleted immediately after it is read — so an
#: over-matched line is an operator reading one extra line, while an
#: under-matched one is evidence that no longer exists anywhere.
DENIAL_MARKERS: tuple[str, ...] = (
    "denied",
    "deny",
    "blocked",
    "refused",
    "forbidden",
    "not allowed",
)

#: Bounds on the retained denial evidence. The log is another process's
#: untrusted output, and it lands in a host evidence file.
MAX_DENIAL_EVENTS = 200
MAX_DENIAL_LINE_CHARS = 500

#: Bound on the recorded mode repairs. A tree whose every mode was restored
#: would otherwise write a half-million-entry list into the evidence.
MAX_RECORDED_MODE_REPAIRS = 200

#: Ledger-stable tokens naming WHY a realization refused publication. Carried
#: on the exception so the run row and the evidence name the same cause.
REFUSAL_DESTINATION_OCCUPIED = "realized_checkout_destination_occupied"
REFUSAL_NO_GIT = "realized_checkout_no_git"
REFUSAL_OVER_QUOTA = "realized_checkout_over_quota"
REFUSAL_INDEX_UNREADABLE = "realized_checkout_index_unreadable"
REFUSAL_DOWNLOAD_FAILED = "realized_checkout_download_failed"
REFUSAL_NO_HOST_ROOM = "realized_checkout_no_host_room"
REFUSAL_SANDBOX_RELEASED = "realized_checkout_sandbox_released"


class RealizedCheckoutRefused(RuntimeError):
    """This download is not a tree kew will publish.

    ``code`` is the stable token naming which check refused, so a reader of
    the run row does not have to parse the sentence.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RealizedCheckout:
    """One downloaded, verified checkout, ready to be published."""

    path: Path
    used_bytes: int
    entries: int
    #: Whether the download kept the index's file modes. False means they were
    #: restored from the index, which ``mode_repairs`` names.
    modes_preserved: bool
    mode_repairs: tuple[str, ...]


@dataclass(frozen=True)
class CheckoutRealization:
    """What one realization attempt produced: a checkout, or why there is none.

    Both halves are kept because both are evidence. ``denials`` is collected
    before the download and survives a refusal: what the gateway blocked is
    often WHY the run ended the way it did.
    """

    checkout: RealizedCheckout | None
    refusal: str
    refusal_code: str
    denials: tuple[str, ...]

    def as_event(self) -> dict[str, object]:
        """The run-evidence event for this realization."""
        checkout = self.checkout
        return {
            "type": "openshell_realized_checkout",
            "path": str(checkout.path) if checkout is not None else None,
            "bytes": checkout.used_bytes if checkout is not None else 0,
            "entries": checkout.entries if checkout is not None else 0,
            "modes_preserved": checkout.modes_preserved if checkout else False,
            "mode_repairs": list(checkout.mode_repairs) if checkout else [],
            "denials": list(self.denials),
            "refusal": self.refusal,
            "refusal_code": self.refusal_code,
        }


def realized_checkout_dir(checkout_dir: Path) -> Path:
    """The host directory one run's realized checkout lands in.

    Derived from the uploaded checkout's path rather than handed down a launch,
    so every caller that holds the checkout can name the realized tree — the
    dispatch that publishes it and the cleanup that removes it included.
    """
    resolved = Path(checkout_dir).expanduser().resolve()
    return resolved.parent / f"{resolved.name}{REALIZED_SUFFIX}"


def retire_realized_checkout(checkout_dir: Path) -> None:
    """Remove the realization beside *checkout_dir* a new launch supersedes.

    The retry loop re-enters the SAME host checkout for every attempt
    (``dispatch_core._run_retry_loop`` threads one ``worktree_dir`` through the
    whole ladder), so the realized sibling the previous attempt left is still
    sitting there when the next one launches. The download refuses an occupied
    destination — correctly, it must never merge into an existing tree — so
    without this every attempt after the first was refused as
    destination-occupied and its work was deleted with its sandbox having never
    been downloaded (PR #2451 review).

    A launch from this checkout is what makes the tree beside it superseded:
    publication reads the realization of the attempt that produced the result
    (``DispatchResult.realized_checkout``), never an earlier attempt's. That
    is why the removal belongs to the LAUNCH and not to the realization — the
    realization's own refusal keeps meaning "something kew did not put here",
    and stays the one stage that deletes nothing.

    Fail-soft. A tree that cannot be removed leaves the occupied refusal to
    fire exactly as it did before, which is a named refusal rather than a
    launch that will not start.
    """
    destination = realized_checkout_dir(checkout_dir)
    if not destination.exists():
        return
    logger.info(
        "the realized checkout at %s is from an earlier attempt on this "
        "checkout and is removed before this launch realizes its own",
        destination,
    )
    try:
        shutil.rmtree(destination)
    except OSError as exc:
        logger.warning(
            "the superseded realized checkout at %s could not be removed "
            "(%s); this attempt's realization will refuse rather than download "
            "onto it",
            destination,
            exc,
        )


def released_realization(name: str, denials: tuple[str, ...]) -> CheckoutRealization:
    """The realization of a sandbox that was already released.

    ``kill`` ends the workload by DELETING the sandbox, so a path that killed
    before it realized — a cap breach, a launch unwind, an interrupt through
    ``ContainerHandle.cancel_launch`` — has nothing left to download from. The
    denials the kill read while the sandbox still existed travel on this
    refusal, which is the whole reason it is a realization and not a silent
    ``None``: that log is the only account of what the gateway refused the
    agent, and it is recorded even though no tree came back (PR #2451 review).
    """
    return CheckoutRealization(
        None,
        f"openshell sandbox {name} was released before its checkout was "
        "downloaded, so the agent's work is not on this host",
        REFUSAL_SANDBOX_RELEASED,
        denials,
    )


def realize_sandbox_checkout(
    client: OpenShellClient,
    name: str,
    *,
    checkout_dir: Path,
    source: str = CHECKOUT_PATH,
    quota_bytes: int = CHECKOUT_QUOTA_BYTES,
    quota_entries: int = CHECKOUT_QUOTA_ENTRIES,
    free_floor: int = HOST_FREE_SPACE_FLOOR_BYTES,
    denials: tuple[str, ...] | None = None,
) -> CheckoutRealization:
    """Download, verify and bind one sandbox's checkout. Never raises.

    Called from the handle's cleanup, which runs on every exit path and
    swallows what it raises — so a failure here has to be RETURNED to be
    acted on. A realization that refused yields no checkout, and the caller
    refuses publication rather than falling back to the uploaded tree: that
    tree holds none of the agent's work, and pushing it would publish an empty
    branch as if the agent had made it.

    ``denials`` is the log the CALLER already read, for the paths where the
    sandbox had to be released before this ran: the handle's kill reads it
    while the sandbox still exists (:func:`collect_denials`), and passing it in
    is what keeps that evidence on the realization rather than re-reading a log
    that is gone. Left ``None``, the read happens here, first, as it does on
    the ordinary cleanup path.
    """
    if denials is None:
        denials = collect_denials(client, name)
    destination = realized_checkout_dir(checkout_dir)
    try:
        checkout = _realize(
            client,
            name,
            source=source,
            destination=destination,
            quota_bytes=quota_bytes,
            quota_entries=quota_entries,
            free_floor=free_floor,
        )
    except RealizedCheckoutRefused as exc:
        logger.error("openshell sandbox %s: %s", name, exc)
        if exc.code != REFUSAL_DESTINATION_OCCUPIED:
            _discard_refused_download(destination)
        return CheckoutRealization(None, str(exc), exc.code, denials)
    except (OpenShellError, OSError, ValueError) as exc:
        # ValueError is the subprocess chokepoint's "git is not on the
        # allowlist", which means this host never ran its tool preflight. A
        # refusal says that and keeps the sandbox's deletion on its own path;
        # letting it out would strand the run on a host condition, after the
        # delete, with nothing recorded.
        message = (
            f"the checkout of openshell sandbox {name} could not be realized "
            f"at {destination} ({exc})"
        )
        logger.error("%s", message)
        _discard_refused_download(destination)
        return CheckoutRealization(None, message, REFUSAL_DOWNLOAD_FAILED, denials)
    return CheckoutRealization(checkout, "", "", denials)


#: Token naming a subtree download that brought nothing back. Distinct from
#: :data:`REFUSAL_DOWNLOAD_FAILED`, which is a transfer that errored: this is a
#: transfer the gateway reported as fine with no tree at the other end.
REFUSAL_SUBTREE_ABSENT = "realized_subtree_absent"


def realize_sandbox_subtree(
    client: OpenShellClient,
    name: str,
    *,
    source: str,
    destination: Path,
    marker: str,
    quota_bytes: int = CHECKOUT_QUOTA_BYTES,
    quota_entries: int = CHECKOUT_QUOTA_ENTRIES,
    free_floor: int = HOST_FREE_SPACE_FLOOR_BYTES,
) -> Path:
    """Download ONE directory out of a sandbox, under the same caps as a checkout.

    The checkout realization above brings back a published git tree; this
    brings back a directory that is deliberately NOT one — the installed
    environment the review-environment provisioner builds inside its own
    sandbox (epic node N12). Everything that bounds the transfer is shared with
    it: the destination must be empty, the host must have room for a checkout at
    the cap before the first byte, and the tree is refused if it breaches the
    byte or entry cap once it has landed.

    ``marker`` is the relative path the download HAS to bring back for its root
    to be identifiable, and it is shared with the checkout realization too:
    ``sandbox download`` may write the source's contents into the destination
    or the source directory itself beneath it, so the landing place is read off
    the marker rather than assumed to be one of the two
    (:func:`_landed_root`). Assuming the nesting refused every provisioning
    download as :data:`REFUSAL_SUBTREE_ABSENT` under the other semantic (PR
    #2520 review).

    What is NOT shared is the index-mode restoration and the prompt-handoff
    removal, and both omissions are the point rather than an oversight: there
    is no index to compare a ``.venv`` against, and reading one would refuse
    every provisioning download as "modes unverifiable" for a tree git has
    never heard of.

    Raises :class:`RealizedCheckoutRefused` — never returns a path that is not
    there, because the caller's next step is to move it into the reviewed
    checkout and a silent miss would promote nothing while reporting success.
    """
    _download_into_empty(
        client,
        name,
        source=source,
        destination=destination,
        quota_bytes=quota_bytes,
        free_floor=free_floor,
    )
    root = _landed_root(
        destination, source, lambda candidate: (candidate / marker).exists()
    )
    if root is None:
        raise RealizedCheckoutRefused(
            f"the download of {source} out of openshell sandbox {name} left no "
            f"directory holding {marker} at or under {destination}",
            code=REFUSAL_SUBTREE_ABSENT,
        )
    _require_within_caps(root, quota_bytes, quota_entries)
    return root


def _discard_refused_download(destination: Path) -> None:
    """Remove a download kew refused, so the refusal costs no host disk.

    The over-cap transfer is why this exists. The tree can only be measured
    once it has LANDED, so a refusal that left it in place would hand the
    agent that filled its sandbox the host's free space anyway — refused
    publication and all — until the dispatch cleanup came past. The removal
    only ever touches a destination this realization created: the one refusal
    that finds a tree already there (:data:`REFUSAL_DESTINATION_OCCUPIED`)
    happens before the download and is excluded by its caller, because that
    tree is not this run's to delete.
    """
    if not destination.exists():
        return
    try:
        shutil.rmtree(destination)
    except OSError as exc:
        logger.warning(
            "the refused download at %s could not be removed (%s); it is left "
            "for the dispatch cleanup",
            destination,
            exc,
        )


def _realize(
    client: OpenShellClient,
    name: str,
    *,
    source: str,
    destination: Path,
    quota_bytes: int,
    quota_entries: int,
    free_floor: int,
) -> RealizedCheckout:
    """The realization itself, in the order the checks have to happen."""
    _download_into_empty(
        client,
        name,
        source=source,
        destination=destination,
        quota_bytes=quota_bytes,
        free_floor=free_floor,
    )
    root = _download_root(destination, source)
    used, entries = _require_within_caps(root, quota_bytes, quota_entries)
    preserved, repairs = restore_index_modes(root)
    # The prompt handoff was uploaded with the checkout, so the download
    # brought it back. Removed here for the same reason the host removes its
    # own copy on every exit path (#725): it must never land in a PR branch.
    remove_prompt_handoff(root)
    return RealizedCheckout(
        path=root,
        used_bytes=used,
        entries=entries,
        modes_preserved=preserved,
        mode_repairs=repairs,
    )


def _download_into_empty(
    client: OpenShellClient,
    name: str,
    *,
    source: str,
    destination: Path,
    quota_bytes: int,
    free_floor: int,
) -> None:
    """Refuse anything already at *destination*, then download into it."""
    if _destination_is_occupied(destination):
        raise RealizedCheckoutRefused(
            f"{destination} already exists and is not an empty directory; a "
            "download into an existing tree keeps the files the agent deleted",
            code=REFUSAL_DESTINATION_OCCUPIED,
        )
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        # mkdir(exist_ok=True) accepts a link to a directory, so one planted
        # since the probe above is refused here rather than downloaded into.
        raise RealizedCheckoutRefused(
            f"{destination} became a symlink before the download; kew never "
            "downloads through one",
            code=REFUSAL_DESTINATION_OCCUPIED,
        )
    _require_host_room(destination, quota_bytes, free_floor)
    client.sandbox_download(name, source, destination)


def _destination_is_occupied(destination: Path) -> bool:
    """Whether anything already sits at *destination* — unreadable counts as yes.

    The probe cannot be allowed to RAISE, because the refusal path that
    follows a raise removes the destination
    (:func:`_discard_refused_download`) and this is the one stage whose
    destination may belong to someone else. A directory kew cannot even list
    is therefore occupied: refused, and left exactly where it is.

    Read without following a symlink (#2626): a pre-created
    ``<checkout>-realized -> /outside-empty`` would otherwise read as the
    empty directory it points at, and the download would land outside the
    checkout's parent. A symlink is occupied whatever it points at, dangling
    included.
    """
    try:
        if not destination.exists(follow_symlinks=False):
            return False
        return (
            destination.is_symlink()
            or not destination.is_dir()
            or any(destination.iterdir())
        )
    except OSError as exc:
        logger.warning(
            "%s could not be inspected (%s); it is treated as occupied, so "
            "this realization refuses rather than downloading onto it",
            destination,
            exc,
        )
        return True


def _require_host_room(destination: Path, quota_bytes: int, free_floor: int) -> None:
    """Refuse a download the host has no room for, before it starts.

    The transfer is the gateway CLI's: kew hands it a destination and gets
    control back when it is done, so there is no chunk to charge against a
    budget the way the docker path's bounded copier charges its own writes
    (``review_environment._BoundedCopier``). The only bound left is the one
    taken BEFORE the first byte — the device has to be able to hold a checkout
    at the cap and still leave the host's free-space floor, the same floor the
    launch already required of this device
    (``sandbox.check_host_capacity``) and the same one the docker watchdog
    enforces while a container runs. A host that cannot is an operator
    condition, named here, rather than an ENOSPC in the middle of a transfer
    that reports as a broken download.

    A failed probe is NOT read as room: it is logged and the download goes
    ahead, because refusing a run over an unreadable ``statvfs`` would discard
    the agent's work over kew's own instrumentation.
    """
    try:
        free = shutil.disk_usage(destination).free
    except OSError as exc:
        logger.warning(
            "the free space of %s could not be probed (%s); the download's "
            "host-room floor is not checked for this realization",
            destination,
            exc,
        )
        return
    if free - quota_bytes < free_floor:
        raise RealizedCheckoutRefused(
            f"the host has {free} bytes free at {destination}, which cannot "
            f"hold a checkout at the {quota_bytes} byte cap and still leave "
            f"the {free_floor} byte free-space floor; the transfer cannot be "
            "stopped halfway, so the room has to be there before it starts",
            code=REFUSAL_NO_HOST_ROOM,
        )


def _landed_root(
    destination: Path, source: str, found: Callable[[Path], bool]
) -> Path | None:
    """Which of the CLI's two copy semantics this download used, or None.

    ``sandbox download`` may write the source's CONTENTS into the destination,
    or the source directory itself beneath it. Both are the same tree, so both
    candidates are offered to *found* — the caller's own proof of what it asked
    for (a checkout's ``.git``, an environment's ``pyvenv.cfg``) — rather than
    either being guessed. ONE answer to that question, so a second caller
    cannot support one semantic while the first supports both (PR #2520
    review).
    """
    for candidate in (destination, destination / PurePosixPath(source).name):
        if found(candidate):
            return candidate
    return None


def _download_root(destination: Path, source: str) -> Path:
    """Where the downloaded checkout landed, proven by its ``.git``."""
    root = _landed_root(destination, source, lambda c: (c / ".git").is_dir())
    if root is not None:
        return root
    raise RealizedCheckoutRefused(
        f"the download of {source} into {destination} kept no .git directory; "
        "a checkout without one cannot be published and is evidence of a "
        "partial transfer",
        code=REFUSAL_NO_GIT,
    )


def _require_within_caps(
    root: Path, quota_bytes: int, quota_entries: int
) -> tuple[int, int]:
    """The realized tree's size, refused when it breaches the checkout caps."""
    used, entries = checkout_usage(
        root, stop_at_bytes=quota_bytes, stop_at_entries=quota_entries
    )
    if used > quota_bytes or entries > quota_entries:
        raise RealizedCheckoutRefused(
            f"the realized checkout {root} breaches the checkout caps "
            f"({used} bytes / {entries} entries vs {quota_bytes} bytes / "
            f"{quota_entries} entries)",
            code=REFUSAL_OVER_QUOTA,
        )
    return used, entries


def restore_index_modes(root: Path) -> tuple[bool, tuple[str, ...]]:
    """Whether the download kept the index's modes, and what was restored.

    ``(True, ())`` is "nothing restored": the modes survived, or the tree has
    nothing indexed to tell from, or the one file that disagrees is the only
    one the index holds on its side — which reads as the agent's own ``chmod``
    and is left alone (:func:`_modes_were_flattened`). ``(False, paths)`` is a
    transfer that dropped mode information, and the paths are the ones
    chmodded back to their index mode — in BOTH directions, because a transfer
    that flattened modes can as easily have made everything executable as
    nothing.
    """
    indexed = _indexed_file_modes(root)
    if not _modes_were_flattened(indexed):
        return True, ()
    return False, _apply_index_modes(root, indexed)


def _modes_were_flattened(indexed: dict[str, tuple[str, int]]) -> bool:
    """Whether this download lost the index's modes rather than an edit did.

    Two signatures, one per direction a flatten can go, and each one is "the
    whole indexed set contradicts the index":

    * every indexed ``100755`` file came back non-executable — the transfer
      that normalizes to ``0o644``;
    * every indexed ``100644`` file came back executable — the transfer that
      normalizes to ``0o755``, which this function's absence let through as
      ``modes_preserved=True`` while a ``100644`` → ``100755`` change on every
      tracked file went to publication (PR #2451 review).

    Neither fires on the agent's own ``chmod``: a single file losing or
    gaining its bit leaves the rest of its side of the index agreeing with
    disk, which is the proof that the transfer carried modes. Only a tree
    whose every file on one side disagrees is a transfer that cannot have —
    and only when that side holds more than one file
    (:data:`_MIN_FLATTEN_EVIDENCE`), since the agent chmodding the single
    indexed executable of a tree produces the identical observation and
    nothing else on that side can break the tie (PR #2451 review).
    """
    executables = _disk_modes(indexed, EXECUTABLE_INDEX_MODE)
    plain = _disk_modes(indexed, PLAIN_INDEX_MODE)
    lost_every_bit = _is_evidence(executables) and not any(
        mode & _EXECUTE_BITS for mode in executables
    )
    gained_every_bit = _is_evidence(plain) and all(
        mode & _EXECUTE_BITS for mode in plain
    )
    return lost_every_bit or gained_every_bit


def _is_evidence(side: list[int]) -> bool:
    """Whether one side of the index holds enough files to read a flatten off."""
    return len(side) >= _MIN_FLATTEN_EVIDENCE


def _disk_modes(indexed: dict[str, tuple[str, int]], index_mode: str) -> list[int]:
    """The on-disk modes of the entries the index records as *index_mode*."""
    return [disk for recorded, disk in indexed.values() if recorded == index_mode]


def _indexed_file_modes(root: Path) -> dict[str, tuple[str, int]]:
    """Each regular file of *root*, as (index mode, mode on disk).

    Both modes come from ONE walk, and the on-disk one from one ``lstat``, so
    the comparison and the repair below cannot disagree about a file that
    changed between two reads of it.

    Absent paths are left out: a file the agent deleted with a plain ``rm`` is
    still in the index, and counting it as "lost its mode" would read a
    deletion as a broken transfer. Symlinks and gitlinks are left out because
    they have no mode to restore — and because ``chmod`` follows a symlink, so
    repairing one would change its target instead.
    """
    from kew.subprocess_utils import run_bytes

    try:
        listed = run_bytes(
            _index_read_command(root),
            env=_index_read_env(),
            timeout=_INDEX_READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        # The read's own timeout, turned into the same refusal its non-zero
        # exit raises. Letting it out would leave the one caller
        # (:func:`realize_sandbox_checkout`, whose handler is typed) with an
        # exception it does not catch: a download that SUCCEEDED would write
        # no realization evidence at all, lose the denial events already
        # collected, keep the undiscarded tree on host disk, and finalize the
        # run saying the work was never downloaded (PR #2451 review).
        raise RealizedCheckoutRefused(
            f"the index of the realized checkout {root} did not come back "
            f"within {_INDEX_READ_TIMEOUT_SECONDS:g}s; its file modes are "
            "unverifiable",
            code=REFUSAL_INDEX_UNREADABLE,
        ) from exc
    if listed.returncode != 0:
        raise RealizedCheckoutRefused(
            f"the index of the realized checkout {root} could not be read "
            f"({_bounded_text(listed.stderr)}); its file modes are unverifiable",
            code=REFUSAL_INDEX_UNREADABLE,
        )
    modes: dict[str, tuple[str, int]] = {}
    for record in listed.stdout.split(b"\0"):
        entry = _index_entry(root, record)
        if entry is not None:
            modes[entry[0]] = entry[1]
    return modes


def _index_read_command(root: Path) -> list[str]:
    """``ls-files --stage`` with every checkout-supplied program disabled.

    The same hardening ``kew.worktree`` applies to its own reads of an
    agent-written tree, spelled here rather than imported from it: that module
    sits above the dispatch layer this one is reached from, and importing it
    would route the sandbox stack back up through it (``lint-imports``). What
    cannot be duplicated is the git ENVIRONMENT, so that is shared
    (:func:`kew.publication_apply.trusted_git_env`) — an inherited ``GIT_DIR``
    would answer about an entirely different repository.
    """
    return [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(root),
        "ls-files",
        "--stage",
        "-z",
    ]


def _index_read_env() -> dict[str, str]:
    """The git environment the index read runs under: no ambient redirects."""
    return trusted_git_env(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )


def _index_entry(root: Path, record: bytes) -> tuple[str, tuple[str, int]] | None:
    """One ``ls-files --stage`` record as (path, (index mode, disk mode)).

    None covers every record this module has nothing to do with: the trailing
    empty one, a mode it does not restore, a path that does not stay inside
    the tree, and an entry whose file is absent or is not a regular file.

    The ``OSError`` arm is that last case, and it is deliberately silent: an
    indexed path with nothing on disk is the ordinary shape of an agent
    deleting a tracked file with ``rm``, so it happens on healthy runs and
    carries no fact worth recording. It is in
    ``ratchets/silence_on_failure_baseline.txt`` for that reason.
    """
    if not record:
        return None
    meta, _, raw_path = record.partition(b"\t")
    mode = meta.split(b" ")[0].decode("ascii", "replace")
    if mode not in _RESTORABLE_INDEX_MODES or not raw_path:
        return None
    relative = os.fsdecode(raw_path)
    # The index is agent-written. git refuses an entry that escapes the
    # worktree, but this module CHMODS what the index names, so the escape is
    # checked here rather than trusted upstream.
    if _escapes_tree(root, relative):
        return None
    try:
        on_disk = (root / relative).lstat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(on_disk):
        return None
    return relative, (mode, stat.S_IMODE(on_disk))


def _escapes_tree(root: Path, relative: str) -> bool:
    """Whether ``root / relative`` names something outside the realized tree.

    An absolute path and a ``..`` component are the obvious two. The third is
    a SYMLINKED PARENT: a tree carrying ``escape -> /etc`` and an index entry
    for ``escape/crontab`` names a path whose every component is innocent, and
    ``lstat`` refuses to follow only the LAST one — so the entry would be read
    as a regular file and chmodded on the HOST, outside everything this module
    is allowed to touch (PR #2451 review). Every parent is therefore checked
    with ``is_symlink``, which follows nothing.

    Deliberately conservative: a symlink that points back INSIDE the tree is
    refused too. The cost of refusing one is the mode of one file not being
    restored; the cost of allowing one is a chmod anywhere the gateway's
    transfer could aim a link.
    """
    parts = PurePosixPath(relative).parts
    if os.path.isabs(relative) or ".." in parts:
        return True
    walked = root
    for part in parts[:-1]:
        walked = walked / part
        if walked.is_symlink():
            return True
    return False


def _apply_index_modes(
    root: Path, indexed: dict[str, tuple[str, int]]
) -> tuple[str, ...]:
    """Chmod every entry to its index mode; return the paths actually changed.

    Every key here came through :func:`_escapes_tree`, so the ``chmod`` — which
    does follow symlinks — is applied to a path whose parents are real
    directories under *root* and whose last component was ``lstat``\\ ed as a
    regular file.
    """
    repaired: list[str] = []
    for relative, (index_mode, current) in sorted(indexed.items()):
        wanted = _mode_for_index(current, index_mode)
        if wanted == current:
            continue
        try:
            (root / relative).chmod(wanted)
        except OSError as exc:
            logger.warning(
                "the mode of %s could not be restored (%s); it keeps the one "
                "the download gave it",
                root / relative,
                exc,
            )
            continue
        if len(repaired) < MAX_RECORDED_MODE_REPAIRS:
            repaired.append(relative)
    return tuple(repaired)


def _mode_for_index(current: int, index_mode: str) -> int:
    """*current* with its execute bits set to what *index_mode* says.

    Execute follows READ, which is how git spells a mode change: a file
    readable only by its owner stays that way and gains ``u+x`` alone, rather
    than being widened to ``0o755`` by a repair nobody asked for.
    """
    if index_mode == EXECUTABLE_INDEX_MODE:
        return current | ((current & _READ_BITS) >> 2)
    return current & ~_EXECUTE_BITS


def collect_denials(client: OpenShellClient, name: str) -> tuple[str, ...]:
    """The sandbox's denial events, read while the sandbox still exists.

    A log kew could not read is NOT reported as "no denials": it is an absent
    observation, logged as one, because the sandbox it would have come from is
    deleted seconds later and nothing can go back for it.
    """
    try:
        text = client.logs(name)
    except OpenShellError as exc:
        logger.warning(
            "openshell sandbox %s logs could not be read (%s); this run keeps "
            "no record of what the gateway denied it",
            name,
            exc,
        )
        return ()
    return denial_events(text)


def denial_events(text: str) -> tuple[str, ...]:
    """The lines of a sandbox log that read as the gateway denying something.

    Credentials are redacted on the way out: these lines are another process's
    output and they land in a host evidence file.
    """
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not any(marker in line.casefold() for marker in DENIAL_MARKERS):
            continue
        found.append(redact_credentials(line)[:MAX_DENIAL_LINE_CHARS])
        if len(found) == MAX_DENIAL_EVENTS:
            break
    return tuple(found)


def _bounded_text(stderr: bytes) -> str:
    """A short, redacted rendering of a failed plumbing call's stderr."""
    return redact_credentials(
        stderr.decode("utf-8", "replace").strip()[:MAX_DENIAL_LINE_CHARS]
    )
