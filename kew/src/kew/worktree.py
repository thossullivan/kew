"""Git worktree lifecycle management.

Creates isolated worktrees for each dispatched issue and cleans them
up on success or preserves them on failure for debugging.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, TYPE_CHECKING, Any, Literal, TypeGuard
from urllib.parse import quote, unquote
from uuid import uuid4

from kew.config import DispatchConfig
from kew.continuation_recovery import (
    CheckoutAdmissionRefusal as CheckoutAdmissionRefusal,
)
from kew.continuation_recovery import CheckoutGenerationRefusal
from kew.credential_surface import exception_text
from kew.ids import branch_slug, validate_item_id
from kew.progress import _warn
from kew.publication import (
    PublicationError,
    publication_git_env,
    sanitize_publication_error,
    select_publication_mode,
)
from kew.publication_apply import trusted_git_env
from kew.publication_canonical import (
    PUSH_ATTEMPTS,
    PUSH_DELAYS,
    CanonicalNoChange,
    CanonicalPublication,
    CopySync,
    PublicationHeld,
    acceptance_contracts,
    host_commit_identity,
    publication_remote,
    publish_checkout,
    remote_branch_sha,
    remote_commit_is_ancestor,
)
from kew.publication_canonical import (
    republish_agent_head as _republish_agent_head_canonical,
)
from kew.publication_checkout import commit_checkout_worktree, filter_checkout_range
from kew.publication_scan import OutputScanTarget
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.continuation_recovery import CheckoutAbandonment
    from kew.run_store import RunStore

_GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

_logger = logging.getLogger(__name__)


def _item_slug(issue_num: int, item_id: str | None, provider: str) -> str:
    """Path/branch-safe slug for the work item.

    When *item_id* is supplied it is validated at this boundary — a
    non-canonical id (one that could escape into a parent path or an illegal
    branch name) raises ``InvalidItemIdError`` rather than being sanitized —
    then lowercased via :func:`branch_slug`. With no *item_id* the legacy
    numeric ``issue_num`` is used verbatim, so GitHub branches and worktree
    paths stay byte-identical to the pre-``item_id`` code.
    """
    if item_id is None:
        return str(issue_num)
    return branch_slug(validate_item_id(item_id, provider))


@dataclass
class ConflictInfo:
    """Information about unresolved merge conflicts in a worktree."""

    strategy_used: str
    conflicted_files: list[str]
    base_branch: str
    base_sha: str = ""


class BaseIntegrationError(RuntimeError):
    """git could not perform the base integration at all — not a conflict.

    A non-zero ``git merge`` with **no unmerged paths** is provably not a
    conflict state: git cannot report conflicts with no conflicted files. It
    means the merge never happened — no committer identity, a held
    ``index.lock``, a hook refusing, a corrupt object, a missing ref.

    This used to be returned as ``ConflictInfo(conflicted_files=[])``, i.e.
    "there are conflicts, and there are no conflicted files" (#1455). Downstream
    reads ``ConflictInfo`` as "dispatch an agent to resolve these", so an
    operational failure became an agent sent to resolve nothing against a tree
    where the merge had not happened — and git's stderr, which said exactly what
    was wrong, was discarded.
    """

    def __init__(self, base_branch: str, detail: str) -> None:
        self.base_branch = base_branch
        self.detail = detail
        super().__init__(f"could not integrate origin/{base_branch}: {detail}")


class PublicationHeadMovedError(RuntimeError):
    """The publication target moved to a commit outside the review work."""

    def __init__(self, expected_sha: str, actual_sha: str, branch: str = "") -> None:
        self.expected_sha = expected_sha
        self.actual_sha = actual_sha
        branch_detail = f" to branch {branch!r}" if branch else ""
        super().__init__(
            f"refusing to publish{branch_detail}: PR head moved "
            f"({expected_sha[:12]} -> {actual_sha[:12]})"
        )


def _origin_url(repo_root: Path) -> str:
    """*repo_root*'s ``origin`` remote URL (the true GitHub remote), or ``""``
    when it has none.

    Clone preparation sanitizes this URL (:func:`_agent_visible_origin`, which
    also drops surrounding whitespace) before writing its origin. Only Git's
    output newline is removed here. Return ``""`` for repositories without an
    origin.
    """
    result = run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        check=False,
    )
    return result.stdout.removesuffix("\n") if result.returncode == 0 else ""


_ORIGIN_SCHEME_URL = re.compile(
    r"([a-z][a-z0-9+.-]*://)([^/?#]*)(.*)", re.IGNORECASE | re.DOTALL
)
# Git's SSH transports end the authority at the first slash only: "?" and "#"
# inside a password are not delimiters there (#1949 round 5).
_ORIGIN_SSH_URL = re.compile(
    r"([a-z][a-z0-9+.-]*://)([^/]*)(.*)", re.IGNORECASE | re.DOTALL
)
_ORIGIN_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})
# Git's native transports (ssh, git, file) percent-decode the URL and then end
# the authority at the first slash; the curl transports split first.
_ORIGIN_NATIVE_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git", "git", "file"})
_ORIGIN_CURL_AUTHORITY = re.compile(r"([^/?#]*)(.*)", re.DOTALL)
_ORIGIN_HELPER_PREFIX = re.compile(r"([^:/?#@\s]+::)(.+)", re.DOTALL)
_ORIGIN_SCP_STYLE = re.compile(r"[^/@:\s]+@(?:\[[^\]\s]+\]|[^/@:\s]+):.*", re.DOTALL)
# Characters that stay literal when a decoded host is re-encoded: an IPv6
# bracket pair, the port separator, and the unreserved set.
_ORIGIN_HOST_SAFE = "[]:.-_~"


def _is_filesystem_origin(url: str) -> bool:
    """An explicit filesystem path (the only non-URL form allowed to carry '@')."""
    return (
        url.startswith(("/", "./", "../", "~/"))
        or re.match(r"^[A-Za-z]:[\\/]", url) is not None
    )


def _agent_visible_origin(url: str) -> str:
    """Remove URL credentials from an origin, or refuse an origin that cannot be
    parsed well enough to prove it carries none.

    Rules (rounds 2-4 of the #1949 review each defeated a looser version):

    * Surrounding whitespace is dropped; no valid remote URL carries any.
    * A remote-helper prefix (``https::<address>``) wraps an address that is
      sanitized on its own, whitespace and all.
    * A scheme URL's authority is percent-decoded before the userinfo split
      (Git decodes it the same way, so ``user%3Apw%40host`` and
      ``user%3Apw@host`` are the same credential); the rebuilt host is
      re-encoded so it still names the destination Git would contact, and an
      SSH username is kept, re-encoded.
    * An scp-style path (``git@host:path``) keeps its username.
    * An explicit filesystem path (``/``, ``./``, ``../``, ``~/``, a drive
      letter) is returned as is.
    * Anything else that contains ``@`` is refused without echoing it: quoted
      URLs, helper prefixes with inner whitespace, bare ``user:pw@host/…``,
      and relative paths whose ``@`` could be userinfo.
    * The rebuilt URL must still name a host and must come back unchanged
      from a second pass: an authority that is only userinfo
      (``https://u:p@/user:pw@host/r.git``) would otherwise move the
      credential behind a third slash (#1949 round 8). An encoded delimiter
      inside the userinfo is a password character; inside a curl host it is
      ambiguous and refuses.
    * A curl authority with a bracketed IPv6 literal splits its userinfo on
      the raw text: curl never decodes the literal, so an encoded ``@`` in a
      zone identifier (``[fe80::1%25eth%400]``) is host data, not a
      separator (#1949 round 9). The literal itself must be an IPv6 address
      with an optional percent-encoded zone identifier; raw userinfo inside
      the brackets (``[user:pw@host]``) and anything else around them
      refuses (#1949 round 10).
    """
    url = url.strip()
    helper_match = _ORIGIN_HELPER_PREFIX.fullmatch(url)
    if helper_match:
        helper, address = helper_match.groups()
        return f"{helper}{_agent_visible_origin(address)}"
    match = _ORIGIN_SCHEME_URL.fullmatch(url)
    if match is None:
        return _non_url_origin(url)
    return _scheme_origin(url, match.group(1))


def _non_url_origin(url: str) -> str:
    """A value without a scheme: scp-style and explicit filesystem paths pass
    through; anything else carrying "@" is refused without echoing it."""
    if "@" not in url or _ORIGIN_SCP_STYLE.fullmatch(url) or _is_filesystem_origin(url):
        return url
    raise RuntimeError("cannot prepare checkout: unparseable origin userinfo")


def _split_native_authority(rest: str) -> tuple[str, str]:
    """Split a native-transport URL body at the first literal or encoded slash.

    Git decodes ``%2F`` before it separates the authority, so
    ``oldhost:2222%2Fnamespace@newhost/r.git`` has the authority
    ``oldhost:2222`` and no userinfo; splitting on the raw string keeps the
    suffix's original encoding so the destination Git computes is unchanged.
    """
    i = 0
    while i < len(rest):
        if rest[i] == "/" or (rest[i] == "%" and rest[i + 1 : i + 3].lower() == "2f"):
            break
        i += 1
    return rest[:i], rest[i:]


def _split_authority(name: str, rest: str) -> tuple[str, str]:
    """Raw authority and suffix of a scheme URL body, per transport family.

    Native transports (ssh family, git, file) end the authority at the first
    slash, decoded or literal. The curl transports split at ``/``, ``?`` or
    ``#`` before decoding; an empty curl authority followed by ``@`` means the
    credential travels after a third slash (``https:///user:pw@host``) and
    refuses (#1949 round 7).
    """
    if name in _ORIGIN_NATIVE_SCHEMES:
        return _split_native_authority(rest)
    curl_match = _ORIGIN_CURL_AUTHORITY.fullmatch(rest)
    assert curl_match is not None  # the pattern accepts every string
    raw_authority, suffix = curl_match.groups()
    if not raw_authority and "@" in suffix:
        raise RuntimeError("cannot prepare checkout: unparseable origin userinfo")
    return raw_authority, suffix


def _decode_authority(raw_authority: str) -> str:
    """Percent-decode an authority strictly; refuse what cannot be rebuilt.

    A non-UTF-8 escape would become a replacement character (#1949 round 5).
    """
    try:
        return unquote(raw_authority, errors="strict")
    except UnicodeDecodeError:
        raise RuntimeError(
            "cannot prepare checkout: unparseable origin userinfo"
        ) from None


def _check_host(name: str, host: str) -> None:
    """Refuse a decoded host that is empty or, on a curl transport, carries a
    delimiter.

    curl splits before decoding, so ``host%2Fx`` is ambiguous (#1949 round 6);
    a delimiter inside the userinfo is only a password character. An empty
    host after the userinfo split would leave the rest of the URL where curl
    looks for the authority (#1949 round 8).
    """
    ambiguous = name not in _ORIGIN_NATIVE_SCHEMES and any(ch in host for ch in "/?#")
    if not host or ambiguous:
        raise RuntimeError("cannot prepare checkout: unparseable origin userinfo")


_ORIGIN_BRACKET_PORT = re.compile(r"(:[0-9]*)?")
# RFC 3986 IP-literal restricted to IPv6address with a zone id, either the
# RFC 6874 form ("%25" then unreserved or pct-encoded characters) or curl's
# raw form ("%en0", accepted by libcurl and Git; #1949 round 11). Git's
# credential parser reads a raw "@" inside brackets as userinfo, so nothing
# else is allowed.
_ORIGIN_IPV6_LITERAL = re.compile(
    r"\[[0-9A-Fa-f:.]+(?:%(?:25)?(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)?\]"
)


def _split_bracketed_curl_authority(raw_authority: str) -> tuple[str, str, str]:
    """Userinfo, separator and final host of a curl authority holding ``[…]``.

    The literal and its port stay exactly as written (curl does not decode
    them); only the text before the bracket is decoded and may carry the
    credential. Any other arrangement of the brackets refuses.
    """
    refuse = RuntimeError("cannot prepare checkout: unparseable origin userinfo")
    start = raw_authority.rfind("[")
    end = raw_authority.find("]", start)
    if end < 0 or "[" in raw_authority[:start] or "]" in raw_authority[:start]:
        raise refuse
    literal, port = raw_authority[start : end + 1], raw_authority[end + 1 :]
    if (
        _ORIGIN_IPV6_LITERAL.fullmatch(literal) is None
        or _ORIGIN_BRACKET_PORT.fullmatch(port) is None
    ):
        raise refuse
    userinfo, separator, tail = _decode_authority(raw_authority[:start]).rpartition("@")
    if tail:
        # Something other than the separator sits between userinfo and host.
        raise refuse
    return userinfo, separator, f"{literal}{port}"


def _split_userinfo(name: str, raw_authority: str) -> tuple[str, str, str]:
    """Userinfo, separator and the re-encoded host of a decoded authority."""
    if name not in _ORIGIN_NATIVE_SCHEMES and "[" in raw_authority:
        userinfo, separator, host = _split_bracketed_curl_authority(raw_authority)
    else:
        userinfo, separator, host = _decode_authority(raw_authority).rpartition("@")
        if separator:
            _check_host(name, host)
            host = quote(host, safe=_ORIGIN_HOST_SAFE)
    return userinfo, separator, host


def _strip_scheme_userinfo(url: str, scheme: str) -> str:
    """One pass of userinfo removal; SSH transports keep their username."""
    name = scheme[:-3].lower()
    raw_authority, suffix = _split_authority(name, url[len(scheme) :])
    userinfo, separator, host = _split_userinfo(name, raw_authority)
    if not separator:
        return url
    if name in _ORIGIN_SSH_SCHEMES:
        username = quote(userinfo.partition(":")[0], safe="")
        host = f"{username}@{host}"
    return f"{scheme}{host}{suffix}"


def _scheme_origin(url: str, scheme: str) -> str:
    """Strip userinfo from a scheme URL and prove the result is stable.

    A rebuilt URL that a second pass would change (or refuse) still carries
    something the first pass mistook for a host or a path; refuse it rather
    than write it as the agent's origin (#1949 round 8).
    """
    rebuilt = _strip_scheme_userinfo(url, scheme)
    if rebuilt != url and _strip_scheme_userinfo(rebuilt, scheme) != rebuilt:
        raise RuntimeError("cannot prepare checkout: unparseable origin userinfo")
    return rebuilt


def _has_commit(repo: Path, ref: str) -> bool:
    """True if *ref* resolves to a commit object present in *repo*."""
    return (
        run(
            ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


def _resolve_commit(repo: Path, ref: str) -> str:
    """Resolve and peel *ref* to a commit, or return an empty string."""
    resolved = run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ],
        cwd=repo,
        check=False,
    )
    oid = (resolved.stdout or "").strip() if resolved.returncode == 0 else ""
    return oid if _GIT_OID.fullmatch(oid) else ""


def _fetch_commit_from_origin(repo: Path, ref: str) -> str:
    """Fetch *ref* from origin and return its peeled commit.

    The fetch lands in a ref unique to this call, never in FETCH_HEAD: the
    operator repository is shared by concurrent dispatches and reviewers, and
    a FETCH_HEAD read after another process's fetch would pin its commit.
    """
    landing = f"refs/kew/bootstrap/{uuid4().hex}"
    try:
        run(
            [
                "git",
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                f"{ref}:{landing}",
            ],
            cwd=repo,
            check=True,
        )
        oid = _resolve_commit(repo, landing)
    finally:
        # Value-bound delete against the ref's own value, which is a tag
        # object rather than the peeled commit when *ref* names an annotated
        # tag. In finally, so a failure after the fetch leaves no ref behind.
        raw = run(
            ["git", "rev-parse", "--verify", "--quiet", landing],
            cwd=repo,
            check=False,
        ).stdout.strip()
        if raw:
            _delete_temporary_ref(repo, landing, raw)
    if not oid:
        raise RuntimeError(f"fetched ref {ref!r} did not resolve to a commit")
    return oid


def _create_temporary_ref(repo: Path, ref: str, oid: str) -> None:
    """Create an operator transfer ref without replacing an existing ref."""
    run(
        ["git", "update-ref", ref, oid, "0" * len(oid)],
        cwd=repo,
        check=True,
    )


def _delete_temporary_ref(repo: Path, ref: str, oid: str) -> None:
    """Delete an operator transfer ref only while it still has *oid*."""
    run(["git", "update-ref", "-d", ref, oid], cwd=repo, check=False)


def _required_commit(repo_root: Path, ref: str, *, has_origin: bool) -> str:
    """The commit *ref* names for a narrow clone.

    A full object id resolves locally first: a cached PR head works offline,
    and the id itself fixes the closure. A NAMED ref resolves on origin
    whenever there is one: the operator's local branch of that name can carry
    unpushed commits (a secret committed and never pushed), and its closure
    must not reach the agent.
    """
    if _GIT_OID.fullmatch(ref) or not has_origin:
        oid = _resolve_commit(repo_root, ref)
        if oid:
            return oid
        if not has_origin:
            raise RuntimeError(f"local ref {ref!r} did not resolve to a commit")
    return _fetch_commit_from_origin(repo_root, ref)


def resolve_base_commit(repo_root: Path, ref: str) -> str:
    """The commit a checkout of *repo_root* pinned at *ref* would hold.

    :func:`_required_commit`'s rule, resolved once so a caller can read other
    facts (the reviewer prompt, #3087) off the same commit it then clones.
    """
    return _required_commit(repo_root, ref, has_origin=bool(_origin_url(repo_root)))


def _pin_required_commits(
    repo_root: Path,
    required_refs: tuple[str, ...],
    transfer_prefix: str,
    transfer_refs: list[tuple[str, str]],
) -> None:
    """Resolve required commits and pin each under an operator transfer ref."""
    has_origin = bool(_origin_url(repo_root))
    for index, ref in enumerate(required_refs):
        oid = _required_commit(repo_root, ref, has_origin=has_origin)
        branch = transfer_prefix if index == 0 else f"{transfer_prefix}-{index}"
        transfer_ref = f"refs/heads/{branch}"
        _create_temporary_ref(repo_root, transfer_ref, oid)
        transfer_refs.append((transfer_ref, oid))


def _clone_pinned_commits(
    repo_root: Path,
    checkout_dir: Path,
    transfer_refs: list[tuple[str, str]],
) -> str:
    """Clone the first pin and transfer any remaining pinned commits."""
    transfer_branch = transfer_refs[0][0].removeprefix("refs/heads/")
    run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--no-hardlinks",
            "--no-tags",
            "--single-branch",
            "--branch",
            transfer_branch,
            str(repo_root),
            str(checkout_dir),
        ],
        check=True,
    )
    if len(transfer_refs) > 1:
        run(
            [
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                str(repo_root),
                *(ref for ref, _oid in transfer_refs[1:]),
            ],
            cwd=checkout_dir,
            check=True,
        )
    return transfer_branch


def _require_pinned_commits_transferred(
    checkout_dir: Path, transfer_refs: list[tuple[str, str]]
) -> None:
    """Require every operator pin to exist in the new checkout."""
    for _ref, oid in transfer_refs:
        if not _has_commit(checkout_dir, oid):
            raise RuntimeError(
                f"required commit {oid} was not transferred to {checkout_dir}"
            )


def _scrub_clone_bootstrap(checkout_dir: Path, transfer_branch: str) -> None:
    """Remove the clone's temporary branch, tracking ref, and fetch config."""
    run(
        ["git", "checkout", "--detach", "--quiet", "HEAD"],
        cwd=checkout_dir,
        check=True,
    )
    run(
        ["git", "update-ref", "-d", f"refs/heads/{transfer_branch}"],
        cwd=checkout_dir,
        check=True,
    )
    run(
        ["git", "update-ref", "-d", f"refs/remotes/origin/{transfer_branch}"],
        cwd=checkout_dir,
        check=True,
    )
    run(
        ["git", "config", "--remove-section", f"branch.{transfer_branch}"],
        cwd=checkout_dir,
        check=True,
    )
    run(
        ["git", "config", "--unset-all", "remote.origin.fetch"],
        cwd=checkout_dir,
        check=True,
    )


def clone_local_checkout(
    repo_root: Path, checkout_dir: Path, required_ref: str | Iterable[str]
) -> tuple[str, ...]:
    """Clone only the required refs into *checkout_dir* and sanitize ``origin``.

    The single clone + upstream seam that BOTH sandbox dispatch
    (``dispatch_core._materialize_checkout``) and the reviewer
    (``review_dispatch.materialize_reviewer_worktree``) share, so the git-root
    clone (#1002) and the upstream setup that makes a remote-only-head fetch
    possible (#1004) live in exactly one place. Temporary branches pin the
    commits whose reachable histories the checkout initially needs.
    ``--single-branch --no-local`` forces Git's upload-pack transport, so
    unrelated refs and unreachable
    objects in the operator repository never enter the checkout. The checkout
    remains self-contained, with its own ``.git`` and object store. Later named
    fetches use the operator repository through :func:`fetch_ref`. A *repo_root*
    without an origin keeps the clone-source origin, which supports local-only
    repositories.
    Return the peeled commits in the caller's requested order.
    """
    # --no-hardlinks is load-bearing, not a performance knob. A local `git
    # clone` HARDLINKS object files by default, so the clone's
    # `.git/objects/<xx>/<sha>` is literally the same inode as the source's
    # (links=2). An agent handed such a checkout can chmod one and write
    # through it, corrupting the OPERATOR's repository — verified: `git fsck`
    # in the source then reports "object corrupt or missing". Giving the agent
    # its own `.git` means nothing if the object store underneath is shared.
    required_refs = (
        (required_ref,) if isinstance(required_ref, str) else tuple(required_ref)
    )
    if not required_refs:
        raise ValueError("clone_local_checkout requires at least one ref")

    operator_origin = _origin_url(repo_root)
    upstream = _agent_visible_origin(operator_origin)
    transfer_prefix = f"kew-bootstrap-{uuid4().hex}"
    transfer_refs: list[tuple[str, str]] = []
    try:
        _pin_required_commits(repo_root, required_refs, transfer_prefix, transfer_refs)
        transfer_branch = _clone_pinned_commits(repo_root, checkout_dir, transfer_refs)
        _require_pinned_commits_transferred(checkout_dir, transfer_refs)
        _scrub_clone_bootstrap(checkout_dir, transfer_branch)
    finally:
        for transfer_ref, oid in transfer_refs:
            _delete_temporary_ref(repo_root, transfer_ref, oid)
    if upstream:
        run(
            ["git", "remote", "set-url", "origin", upstream],
            cwd=checkout_dir,
            check=True,
        )
    _inherit_committer_identity(repo_root, checkout_dir)
    return tuple(oid for _ref, oid in transfer_refs)


def _inherit_committer_identity(repo_root: Path, checkout_dir: Path) -> None:
    """Carry *repo_root*'s effective committer identity into the clone.

    ``git clone`` copies objects and refs, never config. A repository whose
    ``user.email`` is set only LOCALLY therefore produces a clone that cannot
    commit at all ("unable to auto-detect email address"), and every commit kew
    brokers in that checkout fails — on a machine with a global identity this is
    invisible, which is exactly why it needs to be handled here rather than
    discovered per call site.

    Read effectively (local, then global, then system), so a clone commits as
    whoever the source repository commits as. Absent identity stays absent: if
    the source cannot commit either, inventing a name here would only move the
    failure somewhere less obvious.
    """
    for key in ("user.name", "user.email"):
        found = run(["git", "-C", str(repo_root), "config", "--get", key], check=False)
        value = found.stdout.strip() if found.returncode == 0 else ""
        if value:
            run(["git", "config", key, value], cwd=checkout_dir, check=False)


def fetch_ref(checkout_dir: Path, ref: str, *, repo_root: Path | None = None) -> None:
    """Fetch upstream through operator-owned authentication, then transfer locally.

    A unique operator ref pins each transfer against concurrent fetches.
    Only the operator repository contacts origin. The checkout receives a local
    path and ref, so its config and FETCH_HEAD contain no upstream credentials.
    Branch fetches also refresh the checkout's origin tracking ref.
    """
    operator = (repo_root if repo_root is not None else _repo_root()).resolve()
    transfer_ref = f"refs/kew/bootstrap/{uuid4().hex}"
    transfer_oid = ""
    created = False
    try:
        if _origin_url(operator):
            transfer_oid = _fetch_commit_from_origin(operator, ref)
        else:
            transfer_oid = _resolve_commit(operator, ref)
            if not transfer_oid:
                raise RuntimeError(f"local ref {ref!r} did not resolve to a commit")
        _create_temporary_ref(operator, transfer_ref, transfer_oid)
        created = True
        refspec = transfer_ref
        if not _GIT_OID.fullmatch(ref):
            branch = ref.removeprefix("refs/heads/")
            refspec = f"+{transfer_ref}:refs/remotes/origin/{branch}"
        run(
            ["git", "fetch", "--no-tags", str(operator), refspec],
            cwd=checkout_dir,
            check=True,
        )
    finally:
        if created:
            _delete_temporary_ref(operator, transfer_ref, transfer_oid)


def fetch_missing_head(
    checkout_dir: Path, ref: str, *, repo_root: Path | None = None
) -> None:
    """Ensure *ref* is present through an operator-owned upstream fetch.

    Fetch when the local clone lacks it (#1004: a PR head pushed only to the
    remote, which a local clone of repo_root has never seen).

    A no-op when *ref* is already local, so a head the clone already carries
    needs no network round-trip. Delegates the actual fetch to :func:`fetch_ref`
    so the reviewer and dispatch materialize share the one remote-head-fetch step.
    """
    if _has_commit(checkout_dir, ref):
        return
    fetch_ref(checkout_dir, ref, repo_root=repo_root)


def remote_branch_exists(branch_name: str) -> bool:
    """Check if a branch exists on the remote (origin).

    Used to detect if another dispatch process has already started
    working on the same issue.
    """
    result = run(
        ["git", "ls-remote", "--heads", "origin", branch_name],
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def detect_base_branch(config_base: str, *, repo_root: Path | None = None) -> str:
    """Determine the default branch from origin.

    If config_base is set, uses that. Otherwise auto-detects from
    git symbolic-ref, falling back to 'main'.

    *repo_root* names the repository to detect in. Without it the detection
    answers for the process's working directory, which is what dispatch — which
    runs from the repository it dispatches for — wants. A host-side read *about
    the operator repository* must name it (#2177): an operator running ``kew
    publication attest`` from some other checkout would otherwise hand the
    attestation policy whichever base branch THAT repository chose, and the
    policy would accept a merge of an arbitrary origin branch as a base merge.
    """
    if config_base:
        return config_base

    origin_head = "refs/remotes/origin/HEAD"
    if repo_root is None:
        result = run(["git", "symbolic-ref", origin_head], check=False)
    else:
        result = run(
            _checkout_read_command(repo_root, "symbolic-ref", origin_head),
            check=False,
            env=_checkout_read_env(),
        )
    if result.returncode == 0:
        return result.stdout.strip().replace("refs/remotes/origin/", "")
    return "main"


def _generation_sidecar(checkout_dir: Path) -> Path:
    resolved = Path(checkout_dir).expanduser().resolve()
    return resolved.parent / f"{resolved.name}.generation"


def write_checkout_generation(checkout_dir: Path) -> str:
    """Capture complete evidence from kernel identity before worker writes."""
    identity = os.stat(checkout_dir)
    if not stat.S_ISDIR(identity.st_mode):
        raise _generation_refusal("has no checkout directory")
    birth_field, birth_time = _checkout_birth_identity(identity)
    token = uuid4().hex
    nonce = _checkout_generation_nonce(checkout_dir)
    nonce.unlink(missing_ok=True)
    with nonce.open("x", encoding="utf-8") as stream:
        stream.write(token + "\n")
    sidecar = _generation_sidecar(checkout_dir)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    binding = {
        "token": token,
        "nonce": token,
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "birth_field": birth_field,
    }
    if birth_time is not None:
        binding["birth_time"] = birth_time
    evidence = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    sidecar.write_text(evidence + "\n", encoding="utf-8")
    return evidence


def _generation_refusal(reason: str) -> CheckoutGenerationRefusal:
    """Keep binding failures static and provide the supported recovery paths."""
    return CheckoutGenerationRefusal(
        f"checkout generation {reason}; checkout directory binding mismatch; "
        "remove the stale sidecar through kew's revocation helper "
        "or run `kew epic abandon`",
        cause=f"checkout generation {reason}",
    )


def _generation_time_valid(value: object) -> TypeGuard[int | float]:
    """Reject missing, boolean, nonfinite, and negative timestamp identities."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError as exc:
        raise _generation_refusal("has no readable birth identity") from exc


def _checkout_birth_identity(identity: os.stat_result) -> tuple[str, float | None]:
    """Use immutable creation time when available; never bind mutable timestamps.

    typeshed declares ``st_birthtime`` only on the platforms that carry it, and
    kew's own strict type check runs on Linux as well as macOS, so the value is
    read through ``getattr`` rather than as a declared attribute (governed
    review round 5, 2026-09-21).
    """
    value = getattr(identity, "st_birthtime", None)
    if value is None:
        return "none", None
    if not _generation_time_valid(value):
        raise _generation_refusal("has no readable birth identity")
    return "st_birthtime", float(value)


def _checkout_generation_nonce(checkout_dir: Path) -> Path:
    """Keep the nonce inside real checkout metadata; never follow a gitfile or link."""
    metadata = checkout_dir / ".git"
    if not stat.S_ISDIR(os.lstat(metadata).st_mode):
        raise _generation_refusal("has no contained checkout metadata")
    return metadata / "kew-generation"


def _parse_checkout_generation(
    contents: str,
) -> dict[str, str | int | float]:
    """Decode complete evidence; identifier-only historical bindings refuse."""
    try:
        binding = json.loads(contents)
    except ValueError as exc:
        raise _generation_refusal("is malformed") from exc
    if not isinstance(binding, dict):
        raise _generation_refusal("is malformed")
    token = binding.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise _generation_refusal("is malformed")
    nonce = binding.get("nonce")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise _generation_refusal("has no readable nonce binding")
    for key in ("device", "inode"):
        if type(binding.get(key)) is not int or binding[key] < 0:
            raise _generation_refusal("has no readable directory binding")
    _parse_checkout_birth(binding)
    return binding


def _parse_checkout_birth(binding: dict[str, object]) -> tuple[str, float | None]:
    """Require an immutable birth identity or an explicit absent birth field."""
    field = binding.get("birth_field")
    birth = binding.get("birth_time")
    if field == "none" and "birth_time" not in binding:
        return "none", None
    if field != "st_birthtime" or not _generation_time_valid(birth):
        raise _generation_refusal("has no readable birth identity")
    return "st_birthtime", birth


def _bound_checkout_generation(checkout_dir: Path, contents: str) -> str:
    """Read a complete initial snapshot; later boundaries require stored evidence."""
    binding = _parse_checkout_generation(contents)
    _validate_checkout_identity(_checkout_stat(checkout_dir), binding)
    if _read_checkout_nonce(checkout_dir) != binding["nonce"]:
        raise _generation_refusal("does not match the in-checkout nonce")
    return json.dumps(binding, sort_keys=True, separators=(",", ":"))


def _checkout_stat(checkout_dir: Path) -> os.stat_result:
    """Read live identity without invoking checkout Git."""
    try:
        return os.stat(checkout_dir)
    except OSError as exc:
        raise _generation_refusal("is unreadable") from exc


def _validate_checkout_identity(
    identity: os.stat_result, binding: dict[str, str | int | float]
) -> None:
    """Compare kernel identity with evidence captured before the run."""
    if not stat.S_ISDIR(identity.st_mode) or (binding["device"], binding["inode"]) != (
        identity.st_dev,
        identity.st_ino,
    ):
        raise _generation_refusal("does not match the checkout directory")
    field = str(binding["birth_field"])
    if field != "none" and hasattr(identity, field):
        current_birth = getattr(identity, field)
        if (
            not _generation_time_valid(current_birth)
            or binding["birth_time"] != current_birth
        ):
            raise _generation_refusal("does not match the checkout birth identity")


@dataclass(frozen=True)
class CheckoutDirectoryBinding:
    """A lease owns this descriptor and closes it when its run ends."""

    evidence: str
    descriptor: int


def hold_checkout_generation(
    checkout_dir: Path, expected: str
) -> CheckoutDirectoryBinding:
    """Attach one directory descriptor to the run's checkout admission lease."""
    require_checkout_claim(checkout_dir)
    held = _checkout_claims[checkout_dir.absolute()]
    if held.binding is None:
        try:
            descriptor = os.open(
                checkout_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
        except OSError as exc:
            raise _generation_refusal("cannot hold the checkout directory") from exc
        held.binding = CheckoutDirectoryBinding(expected, descriptor)
        try:
            require_checkout_generation(checkout_dir, expected)
        except BaseException:
            os.close(descriptor)
            held.binding = None
            raise
    require_checkout_generation(checkout_dir, expected)
    return held.binding


def require_checkout_generation(checkout_dir: Path, expected: str) -> None:
    """Validate live path and held descriptor against persisted evidence first."""
    binding = _parse_checkout_generation(expected)
    current = _checkout_stat(checkout_dir)
    _validate_checkout_identity(current, binding)
    _validate_checkout_descriptor(checkout_dir, expected, current, binding)
    try:
        contents = _read_generation_sidecar(checkout_dir)
        observed = (
            _parse_checkout_generation(contents) if contents is not None else None
        )
    except CheckoutGenerationRefusal as exc:
        raise _generation_refusal("sidecar is tampered") from exc
    if observed != binding:
        raise _generation_refusal("sidecar is tampered")
    if _read_checkout_nonce(checkout_dir) != binding["nonce"]:
        raise _generation_refusal("does not match the in-checkout nonce")


def _validate_checkout_descriptor(
    checkout_dir: Path,
    expected: str,
    current: os.stat_result,
    binding: dict[str, str | int | float],
) -> None:
    """The held object and path must both retain the captured kernel identity."""
    held = _checkout_claims.get(checkout_dir.absolute())
    if held is None or held.owner != threading.get_ident() or held.binding is None:
        return
    if held.binding.evidence != expected:
        raise _generation_refusal("does not match the held checkout binding")
    try:
        opened = os.fstat(held.binding.descriptor)
    except OSError as exc:
        raise _generation_refusal("has no readable checkout descriptor") from exc
    _validate_checkout_identity(opened, binding)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise _generation_refusal("does not match the held checkout directory")


def _read_checkout_nonce(checkout_dir: Path) -> str:
    """Require a readable regular nonce file inside contained checkout metadata."""
    try:
        nonce = _checkout_generation_nonce(checkout_dir)
        if not stat.S_ISREG(os.lstat(nonce).st_mode):
            raise _generation_refusal("has no regular in-checkout nonce")
        return nonce.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise _generation_refusal("has no readable in-checkout nonce") from exc


def _legacy_generation_identifier(contents: str) -> bool:
    """A sidecar from a build before complete evidence holds one bare token."""
    return re.fullmatch(r"[0-9a-f]{32}", contents.strip()) is not None


def read_checkout_generation(checkout_dir: Path) -> str:
    """Read complete evidence; return "" when no complete identity is bound.

    Validate device, inode, and the in-checkout nonce on every read.
    Validate recorded birth identity only when the platform exposes that field.
    Ignore an unavailable birth field and validate the remaining factors.
    Never bind mutable directory timestamps. An absent sidecar binds nothing.
    A sidecar holding one bare token (a build before complete evidence) proves
    no directory identity either: it reads as unbound, and
    ``capture_checkout_generation`` migrates it once under the checkout claim
    (governed review round 2, 2026-09-21).
    """
    contents = _read_generation_sidecar(checkout_dir)
    if contents is None:
        return ""
    if _legacy_generation_identifier(contents):
        _warn(
            "checkout generation sidecar holds a legacy identifier only; the "
            "identity is bound when the checkout is next captured under its claim"
        )
        return ""
    return _bound_checkout_generation(checkout_dir, contents)


def _read_generation_sidecar(checkout_dir: Path) -> str | None:
    """Read convenience metadata without treating it as persisted identity."""
    sidecar = _generation_sidecar(checkout_dir)
    try:
        mode = os.lstat(sidecar).st_mode
    except FileNotFoundError:
        _warn("checkout generation sidecar is absent; no checkout identity is bound")
        return None
    except OSError as exc:
        raise _generation_refusal("is unreadable") from exc
    if not stat.S_ISREG(mode):
        raise _generation_refusal("is malformed")
    try:
        return sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _generation_refusal("is unreadable") from exc


def capture_checkout_generation(checkout_dir: Path) -> str:
    """Capture complete evidence; mint only an absent sidecar under the claim.

    A legacy one-token sidecar refuses here: this path prepares a checkout a
    run is about to own, and a token that names no directory cannot authorize
    taking one over. Retiring it requires validated abandonment authority
    (:func:`retire_legacy_checkout_generation`), and a resume that adopts the
    checkout it already names uses
    :func:`adopt_legacy_checkout_generation`.
    """
    _require_checkout_generation_claim(checkout_dir)
    contents = _read_generation_sidecar(checkout_dir)
    if contents is not None:
        return _bound_checkout_generation(checkout_dir, contents)
    return _mint_checkout_generation(checkout_dir)


def adopt_legacy_checkout_generation(checkout_dir: Path) -> str:
    """Bind a checkout that predates complete generation evidence.

    Complete evidence is returned unchanged. A sidecar holding one bare token
    names no directory, so a caller that is resuming the checkout it already
    owns binds the directory it is resuming, under its claim, and the next
    read validates it like any other. This is the only path that rewrites a
    legacy identifier without abandonment authority: it adopts a checkout,
    never replaces one (governed review round 2, 2026-09-21).
    """
    _require_checkout_generation_claim(checkout_dir)
    contents = _read_generation_sidecar(checkout_dir)
    if contents is not None and not _legacy_generation_identifier(contents):
        return _bound_checkout_generation(checkout_dir, contents)
    evidence = _mint_checkout_generation(checkout_dir)
    if evidence and contents is not None:
        _warn(
            "checkout generation sidecar held a legacy identifier only; the "
            "resumed checkout is now bound by complete evidence"
        )
    return evidence


def _mint_checkout_generation(checkout_dir: Path) -> str:
    """Write fresh evidence for a present directory; "" when it is absent."""
    try:
        os.stat(checkout_dir)
    except FileNotFoundError:
        _warn("checkout directory is absent; generation minting waits for creation")
        return ""  # Absent candidates acquire a bound token only after creation.
    except OSError as exc:
        raise _generation_refusal("is unreadable") from exc
    try:
        return write_checkout_generation(checkout_dir)
    except OSError as exc:
        raise _generation_refusal("migration failed") from exc


def _require_checkout_generation_claim(checkout_dir: Path) -> None:
    """Require the calling thread to own the checkout before changing its binding."""
    held = _checkout_claims.get(checkout_dir.absolute())
    if held is None or held.owner != threading.get_ident():
        raise CheckoutGenerationRefusal(
            "checkout generation capture requires its claim"
        )


def retire_legacy_checkout_generation(checkout_dir: Path) -> bool:
    """Retire a token-only sidecar after the caller validates abandonment authority."""
    _require_checkout_generation_claim(checkout_dir)
    sidecar = _generation_sidecar(checkout_dir)
    try:
        if not stat.S_ISREG(os.lstat(sidecar).st_mode):
            raise _generation_refusal("is malformed")
        contents = sidecar.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _warn("checkout generation sidecar is absent; no legacy identity to retire")
        return False
    except (OSError, UnicodeError) as exc:
        raise _generation_refusal("is unreadable") from exc
    if re.fullmatch(r"[0-9a-f]{32}", contents) is None:
        return False
    revoke_checkout_generation(checkout_dir)
    return True


def revoke_checkout_generation(checkout_dir: Path) -> None:
    """Remove the authority sidecar and contained nonce without reading the binding."""
    sidecar = _generation_sidecar(checkout_dir)
    try:
        sidecar.unlink(missing_ok=True)
        with contextlib.suppress(FileNotFoundError):
            metadata = checkout_dir / ".git"
            # Gitfiles and links contain no nonce owned by this checkout.
            if stat.S_ISDIR(os.lstat(metadata).st_mode):
                (metadata / "kew-generation").unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"could not retire the checkout identity {sidecar} ({exc}); the "
            "checkout is left in place"
        ) from exc


def remove_checkout_tree(checkout_dir: Path) -> None:
    """Remove a checkout directory kew created, identity first (review round
    18): the one removal path for sandbox checkouts and half-built clones."""
    try:
        revoke_checkout_generation(checkout_dir)
        shutil.rmtree(checkout_dir, ignore_errors=True)
    finally:
        # The lock lives beside the checkout, so it outlived every removed
        # tree: a sandboxed dispatch and a reviewer worktree each left one in
        # the system temp directory under a name nothing reuses (governed
        # review round 5, 2026-09-21). It is removed while this thread still
        # holds it, so no claim another process takes in that window is
        # destroyed (governed review round 6, 2026-09-22).
        _remove_held_checkout_lock(checkout_dir)
        release_checkout_claim(checkout_dir)


def _remove_held_checkout_lock(checkout_dir: Path) -> None:
    """Remove the lock file of a checkout this thread currently claims."""
    key = checkout_dir.absolute()
    held = _checkout_claims.get(key)
    if held is None or held.owner != threading.get_ident():
        return
    with contextlib.suppress(OSError):
        (key.parent / f"{key.name}.lock").unlink(missing_ok=True)


def quarantine_agent_checkout(
    worktree_dir: Path, config: DispatchConfig, *, reason: str
) -> Path | None:
    """Under canonical publication an existing agent checkout is never reused
    through git: it is moved aside with its bytes intact and the caller starts
    from trusted remote state (review round 10 of #1440).

    Returns the preserved path, or None when nothing was there or the escape
    hatch is in use. The rename stays on one filesystem; a failure to move the
    directory is an error, never a reason to run git inside it.
    """
    if config.publication.publish != "canonical" or not worktree_dir.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    preserved = worktree_dir.with_name(f"{worktree_dir.name}.preserved-{stamp}")
    revoke_checkout_generation(worktree_dir)
    try:
        worktree_dir.rename(preserved)
    except OSError as exc:
        raise RuntimeError(
            f"could not move the existing checkout {worktree_dir} aside ({exc}); "
            "canonical publication does not run git inside it, so move or remove "
            "it and retry"
        ) from exc
    _warn(
        f"Canonical publication: {reason}; the existing checkout was moved to "
        f"{preserved} untouched and a fresh clone replaces it"
    )
    return preserved


class CheckoutBusy(RuntimeError):
    """Another run holds the claim on this agent checkout path."""


class CheckoutUnclaimed(CheckoutBusy):
    """The calling thread does not hold the required checkout claim."""


@dataclass
class _CheckoutLease:
    """Run admission owns the flock and its bound checkout descriptor."""

    handle: IO[str]
    owner: int
    binding: CheckoutDirectoryBinding | None = None


_checkout_claims: dict[Path, _CheckoutLease] = {}


def require_checkout_claim(worktree_dir: Path) -> None:
    """Refuse checkout inspection unless this thread owns its lease."""
    held = _checkout_claims.get(worktree_dir.absolute())
    if held is None or held.owner != threading.get_ident():
        raise CheckoutUnclaimed(
            f"{worktree_dir} requires a checkout claim owned by the calling thread"
        )


def claim_checkout(worktree_dir: Path) -> None:
    """One run owns an agent checkout before inspection until the run ends.

    The claim is a non-blocking ``flock`` on a lock file beside the checkout,
    so a dead process releases it, and a concurrent CI fix, resolve or retry
    refuses instead of moving a live checkout aside and leaving the first run
    to publish whatever replaced it. Re-entrant for the owning thread only.
    Both publication modes always acquire the lease.
    """
    key = worktree_dir.absolute()
    held = _checkout_claims.get(key)
    if held is not None:
        if held.owner == threading.get_ident():
            return
        raise CheckoutBusy(f"{worktree_dir} is claimed by another run in this process")
    key.parent.mkdir(parents=True, exist_ok=True)
    handle = _locked_claim_file(key.parent / f"{key.name}.lock", worktree_dir)
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _checkout_claims[key] = _CheckoutLease(handle, threading.get_ident())


#: How many times a claim reopens a lock file that was replaced under it.
_CLAIM_ATTEMPTS = 3


def _locked_claim_file(lock: Path, worktree_dir: Path) -> IO[str]:
    """The claim lock at *lock*, opened and ``flock``-ed by this process.

    Between the open and the ``flock`` the work-root sweep may unlink the
    file; the lock then held is on an inode no other claimer can reach, and
    a second claimer would lock a new file at the same path. So after the
    ``flock`` the held inode must still be the one at *lock*, or the file is
    reopened, a bounded number of times.
    """
    for _attempt in range(_CLAIM_ATTEMPTS):
        handle = open(  # noqa: SIM115 — held until the checkout is discarded
            lock, "a+", encoding="utf-8"
        )
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CheckoutBusy(
                f"{worktree_dir} is claimed by another kew process; wait for that "
                "run to finish before retrying"
            ) from exc
        if _holds_the_file_at(handle, lock):
            return handle
        handle.close()
    raise CheckoutBusy(
        f"{worktree_dir}'s claim lock was replaced {_CLAIM_ATTEMPTS} times while "
        "it was being claimed; retry"
    )


def _holds_the_file_at(handle: IO[str], path: Path) -> bool:
    """Whether *handle* is the file *path* names now (same device and inode)."""
    held = os.stat(handle.fileno())  # a descriptor: the fstat of the held file
    try:
        on_disk = os.stat(path)
    except FileNotFoundError:
        _logger.warning("claim lock %s was removed while it was claimed", path)
        return False
    return (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)


def release_checkout_binding(worktree_dir: Path) -> None:
    """Drop the directory this thread pinned, before replacing that directory.

    A held binding names the exact directory a run admitted, by descriptor and
    by evidence. Removing and restoring the checkout produces a NEW directory,
    so a binding left pinned to the old one refuses every later identity
    boundary on the restored checkout (governed review round 3, 2026-09-21).
    The caller re-pins the restored checkout through its own binding record.
    """
    held = _checkout_claims.get(worktree_dir.absolute())
    if held is None or held.owner != threading.get_ident() or held.binding is None:
        return
    descriptor = held.binding.descriptor
    held.binding = None
    os.close(descriptor)


def release_checkout_claim(worktree_dir: Path) -> None:
    """Drop the calling thread's claim, without releasing a successor's lease."""
    key = worktree_dir.absolute()
    held = _checkout_claims.get(key)
    if held is None or held.owner != threading.get_ident():
        return
    del _checkout_claims[key]
    try:
        if held.binding is not None:
            os.close(held.binding.descriptor)
    finally:
        held.handle.close()


@contextlib.contextmanager
def checkout_claim_guard(worktree_dir: Path) -> Iterator[None]:
    """Hold a checkout lease for the service's abandonment transaction."""
    claim_checkout(worktree_dir)
    try:
        yield
    finally:
        release_checkout_claim(worktree_dir)


def release_checkout_claims() -> None:
    """Drop every claim the calling thread holds: a run's lease ends with the
    run, whether the checkout was discarded or preserved (review round 12).
    The preserved bytes outlive the lease; the next run claims them afresh."""
    me = threading.get_ident()
    for key in [k for k, lease in _checkout_claims.items() if lease.owner == me]:
        release_checkout_claim(key)


def dispatch_checkout_path(
    config: DispatchConfig,
    *,
    issue_num: int,
    item_id: str | None = None,
    provider: str = "github",
) -> Path:
    """Where a host dispatch of this item keeps its checkout."""
    slug = _item_slug(issue_num, item_id, provider)
    return Path(config.branches.worktree_base) / "agent" / f"issue-{slug}"


def setup_worktree(
    *,
    issue_num: int,
    config: DispatchConfig,
    item_id: str | None = None,
    provider: str = "github",
    base_sha: str = "",
    abandonment: CheckoutAbandonment | None = None,
) -> Path:
    """Create an isolated checkout for the issue. Returns its path.

    A standalone CLONE, not ``git worktree add`` (W1.1 / D2, doors 2-4). A
    linked worktree's ``.git`` points into the operator's repository, so an
    agent working here writes the operator's config and hooks — traps that fire
    on the operator's own interactive git commands, with no kew dispatch needed
    to trigger them.

    *item_id* threads the opaque work-item id (A5a): when given, the branch
    name and worktree directory derive from ``branch_slug(item_id)`` instead
    of the numeric issue number. GitHub ids (``"773"``) slug to themselves, so
    behaviour is unchanged for the classic path.
    """
    slug = _item_slug(issue_num, item_id, provider)
    branch_name = f"{config.branches.prefix}{slug}"
    worktree_dir = dispatch_checkout_path(
        config, issue_num=issue_num, item_id=item_id, provider=provider
    )
    claim_checkout(worktree_dir)

    # Clean up a stale checkout from a previous run — but never over work.
    #
    # The dirty refusal predates the clone conversion. The unpushed-commit
    # refusal REPLACES `_clean_stale_branch`, which guarded the same thing from
    # the other side: with a linked worktree the branch lived in the operator's
    # repository, and that check refused to delete it when it was ahead of
    # origin. In a clone the branch lives in the checkout, so discarding the
    # directory discards those commits — the protection has to move with them
    # or it silently disappears.
    if worktree_dir.exists() and is_linked_worktree(worktree_dir):
        raise RuntimeError(
            f"refusing to replace linked worktree at {worktree_dir}; push "
            "the branch or copy the directory, then remove it and retry"
        )
    _quarantine_abandoned_checkout(worktree_dir, branch_name, abandonment)
    # A linked worktree is refused above in every mode: moving one aside would
    # break the operator repository it shares. Any other existing checkout is
    # moved aside untouched under canonical publication; the git-based
    # refusals below are the escape hatch's (review round 10 of #1440).
    if worktree_dir.exists() and (
        quarantine_agent_checkout(
            worktree_dir, config, reason="a previous dispatch left a checkout here"
        )
        is None
    ):
        if not is_safe_standalone_clone(worktree_dir):
            raise RuntimeError(
                f"refusing to replace unsafe checkout at {worktree_dir}; expected "
                "a standalone clone with contained git metadata. Copy any needed "
                "work elsewhere, then remove the checkout and retry"
            )
        if has_uncommitted_changes(worktree_dir):
            raise RuntimeError(
                f"refusing to replace dirty worktree for branch {branch_name!r}"
            )
        repo_root = _repo_root()
        try:
            ahead = unpushed_commit_count(worktree_dir, repo_root, branch_name)
        except Exception as exc:
            raise RuntimeError(
                f"refusing to replace the checkout for branch {branch_name!r}: "
                f"could not verify whether HEAD is on origin ({exc}). Preserve "
                f"or repair the checkout first ({worktree_dir})."
            ) from exc
        if ahead:
            raise RuntimeError(
                f"refusing to replace the checkout for branch {branch_name!r}: "
                f"it has {ahead} commit(s) not on origin. Push or discard them "
                f"first ({worktree_dir})."
            )
        _quarantine_checkout(worktree_dir)

    base_branch = detect_base_branch(config.branches.base)
    run(["git", "fetch", "origin", base_branch, "--quiet"])

    # The stale-local-branch cleanup is gone with the linked worktree. It ran
    # `git branch -D <branch_name>` against the OPERATOR's repository with a
    # name derived from the work item — a destructive host git command taking
    # agent-influenced input (#1445). A clone starts with its own refs, so
    # there is no stale ref to clear and no reason to touch the operator's.
    _clone_checkout_on(
        worktree_dir, branch_name, start_point=base_branch, exact_sha=base_sha
    )
    return worktree_dir


def _quarantine_abandoned_checkout(
    checkout: Path, branch: str, authority: CheckoutAbandonment | None
) -> None:
    """Preserve abandoned bytes only with exact, scoped operator authority."""
    if authority is None:
        return
    if (
        not authority.evidence_identity
        or authority.checkout != checkout.resolve()
        or authority.branch != branch
        or not _abandonment_generation_matches(checkout, authority)
    ):
        raise CheckoutGenerationRefusal(
            "abandonment authority does not match the replacement checkout"
        )
    if checkout.exists():
        if not is_safe_standalone_clone(checkout):
            raise RuntimeError(
                f"refusing to replace unsafe abandoned checkout at {checkout}"
            )
        _quarantine_checkout(checkout)


def _abandonment_generation_matches(
    checkout: Path, authority: CheckoutAbandonment
) -> bool:
    """Absent-path authority can create a candidate, never replace an occupant."""
    if authority.checkout_id:
        return authority.checkout_id == read_checkout_generation(checkout)
    return (
        bool(authority.rebound_from)
        and not os.path.lexists(checkout)
        and not os.path.lexists(_generation_sidecar(checkout))
    )


def _git_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Return bounded Git stderr, or its exit status when stderr is empty."""
    stderr = getattr(result, "stderr", "") or ""
    if stderr.strip():
        return sanitize_publication_error(stderr)
    return f"git exited with status {result.returncode}"


def _operator_origin_url(repo_root: Path) -> str:
    """Return the operator repository's configured origin URL."""
    configured = run(
        [
            "git",
            "-C",
            str(repo_root),
            "config",
            "--local",
            "--get",
            "remote.origin.url",
        ],
        check=False,
        env=trusted_git_env(),
    )
    origin_url = (configured.stdout or "").strip()
    if configured.returncode != 0:
        raise RuntimeError("operator origin URL is unavailable")
    if not origin_url or "\n" in origin_url:
        raise RuntimeError("operator origin URL is invalid")
    return origin_url


def _parse_remote_branch_head(output: str, remote_ref: str) -> str:
    """Parse the one exact branch record expected from ``git ls-remote``."""
    lines = output.splitlines()
    if len(lines) != 1:
        raise RuntimeError(f"operator origin branch {remote_ref!r} is unavailable")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[1] != remote_ref:
        raise RuntimeError("operator origin query returned invalid output")
    if _GIT_OID.fullmatch(fields[0]) is None:
        raise RuntimeError("operator origin query returned an invalid object ID")
    return fields[0]


_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_LIKE_ORIGIN = re.compile(r"^[^/\s]+:")
_OPERATOR_ORIGIN_QUERY_TIMEOUT = 60.0


def _anchor_operator_origin(repo_root: Path, origin_url: str) -> str:
    """Resolve a relative filesystem origin against the operator repository.

    The query runs from a temporary directory, so ``../origin.git`` would be
    resolved against THAT directory and a valid clean checkout could never be
    verified. URL and SCP-style forms are left exactly as configured.
    """
    if _URL_SCHEME.match(origin_url) or _SCP_LIKE_ORIGIN.match(origin_url):
        return origin_url
    path = Path(origin_url).expanduser()
    if path.is_absolute():
        return origin_url
    return str((repo_root / path).resolve())


def _wants_kew_https_auth(origin_url: str) -> bool:
    """Whether the query for *origin_url* should carry kew's own auth header.

    Only an ``https`` origin (scheme compared case-insensitively, as git does)
    that carries no credentials of its own. An origin with ``user:password@``
    already authenticates itself; adding a second Authorization header would
    fight it, and the discovery behind the header runs outside the deadline.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(origin_url)
    except ValueError as exc:
        _warn(f"origin URL could not be parsed; kew auth not attached: {exc}")
        return False
    if parts.scheme.lower() != "https":
        return False
    return parts.username is None and parts.password is None


def _operator_origin_query_env(query_dir: str, origin_url: str) -> dict[str, str]:
    """The sanitized query environment, carrying only kew's own HTTPS auth.

    Global, system and inherited ``GIT_*`` configuration stay out, so no
    ``insteadOf`` rewrite or planted config entry can redirect the query. That
    also drops the credential helpers a private HTTPS origin needs, so the one
    entry kew adds for itself — the ``http.<host>.extraHeader`` authorization —
    is copied back in on its own, never with the inherited entries around it,
    and only for an ``https://`` origin: auth discovery consults the project
    host, which for a filesystem origin is a network fallback with no bound.
    ``GIT_TERMINAL_PROMPT=0`` turns a missing credential into a failure instead
    of a prompt that blocks forever; ``SSH_ASKPASS`` goes too, because git
    runs an askpass program BEFORE that guard applies, and an inherited one is
    a host program this query must never execute.
    """
    env = trusted_git_env(
        GIT_CEILING_DIRECTORIES=query_dir,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)
    # The header is scoped to the origin's authority, but curl forwards a
    # custom header across a followed redirect, so a matching origin that
    # redirects elsewhere would still hand the token to that other host. No
    # redirects: the query answers for the configured origin or not at all.
    _append_git_config(env, "http.followRedirects", "false")
    if not _wants_kew_https_auth(origin_url):
        return env
    auth = publication_git_env()
    if auth is None:
        return env
    last = int(auth.get("GIT_CONFIG_COUNT", "0")) - 1
    key = auth.get(f"GIT_CONFIG_KEY_{last}", "")
    value = auth.get(f"GIT_CONFIG_VALUE_{last}", "")
    if last < 0 or not key.startswith("http.") or not key.endswith(".extraHeader"):
        return env
    scoped_key = _auth_key_for_origin(key, origin_url)
    if scoped_key is None:
        return env
    _append_git_config(env, scoped_key, value)
    return env


def _append_git_config(env: dict[str, str], key: str, value: str) -> None:
    """Add one ``GIT_CONFIG_KEY_n``/``VALUE_n`` pair to *env*'s own list."""
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 1)


def _auth_key_for_origin(key: str, origin_url: str) -> str | None:
    """Re-scope kew's ``http.<url>.extraHeader`` key to the origin's authority.

    The producer builds its key from the project host alone, and git's
    ``--get-urlmatch`` will not match that key against an origin with a
    non-default port or a bracketed IPv6 host. The key is rebuilt from the
    origin's own ``host[:port]`` — but only when the origin's hostname IS the
    project host the header was issued for; the token never travels to any
    other host.
    """
    from urllib.parse import urlsplit

    issued_for = key[len("http.https://") : -len("/.extraHeader")].lower()
    parts = urlsplit(origin_url)
    hostname = (parts.hostname or "").lower()
    # The producer's key carries a bare hostname (no port; an IPv6 address
    # without brackets), so that is what the origin's hostname must equal.
    if not issued_for or hostname != issued_for.strip("[]"):
        return None
    authority = parts.netloc.rsplit("@", 1)[-1]
    return f"http.https://{authority}/.extraHeader"


def _operator_remote_branch_head(repo_root: Path, branch_name: str) -> str:
    """Read *branch_name* from the operator repository's configured origin."""
    return _parse_remote_branch_head(
        _list_operator_remote_branch(repo_root, branch_name),
        f"refs/heads/{branch_name}",
    )


def _operator_branch_published(repo_root: Path, branch_name: str) -> bool:
    """Does the operator repository's origin hold *branch_name* at all?

    ``False`` only when origin answered with no such branch; a query that
    fails, or an answer that is not the one exact record, is raised.
    """
    listed = _list_operator_remote_branch(repo_root, branch_name)
    if not listed.strip():
        return False
    _parse_remote_branch_head(listed, f"refs/heads/{branch_name}")
    return True


def _list_operator_remote_branch(repo_root: Path, branch_name: str) -> str:
    """``git ls-remote`` *branch_name* on the operator's origin; raises on failure."""
    origin_url = _anchor_operator_origin(repo_root, _operator_origin_url(repo_root))
    remote_ref = f"refs/heads/{branch_name}"
    with TemporaryDirectory(prefix="kew-ls-remote-") as query_dir:
        try:
            listed = run(
                ["git", "ls-remote", "--heads", origin_url, remote_ref],
                cwd=Path(query_dir),
                check=False,
                timeout=_OPERATOR_ORIGIN_QUERY_TIMEOUT,
                env=_operator_origin_query_env(query_dir, origin_url),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("operator origin query timed out") from None
    if listed.returncode != 0:
        raise RuntimeError(
            f"operator origin query failed: {_git_failure_detail(listed)}"
        )
    return listed.stdout or ""


def operator_base_branch(config_base: str) -> str:
    """The base branch as the OPERATOR repository names it (#2177).

    The attestation's merge policy turns on this name — a merge whose second
    parent is on the base binds, anything else is refused — so detecting it
    from the process's working directory would let an operator running the
    command from another checkout decide which origin branch counts as "the
    base". A configured base still wins, and answers without a git call.
    """
    if config_base:
        return config_base
    return detect_base_branch(config_base, repo_root=_repo_root())


def operator_commit_parents(repo_root: Path, commit_sha: str) -> tuple[str, ...]:
    """The parents of *commit_sha*, in order, as the operator repository holds it.

    Attesting an operator-pushed head (#2124) binds it through its parents, so
    they have to come from the objects rather than from the operator's word for
    them. Which parent *shapes* are bindable is the attestation's policy
    (#2177), not this read's: a hand-resolved base merge has two parents and
    reports both, in git's order. Only a root commit has nothing to bind
    through, and that is reported as such rather than silently read as
    unparented.
    """
    listed = run(
        _checkout_read_command(
            repo_root, "rev-list", "--parents", "-n", "1", commit_sha
        ),
        check=False,
        env=_checkout_read_env(),
    )
    if listed.returncode != 0:
        raise RuntimeError(
            f"commit {commit_sha} is not in the operator repository: "
            + _git_failure_detail(listed)
        )
    fields = (listed.stdout or "").split()
    if not fields or fields[0] != commit_sha:
        raise RuntimeError(
            f"commit {commit_sha} was not the commit the operator repository "
            "reported parents for"
        )
    parents = tuple(fields[1:])
    if not parents:
        raise RuntimeError(
            f"commit {commit_sha} is a root commit, no parent to bind to"
        )
    if any(_GIT_OID.fullmatch(parent) is None for parent in parents):
        raise RuntimeError(
            f"the parents of commit {commit_sha} could not be read as commit ids"
        )
    return parents


def operator_commit_on_base(repo_root: Path, commit_sha: str, base_branch: str) -> bool:
    """Whether *commit_sha* is reachable from the freshly fetched upstream base.

    An operator who resolves a base conflict by hand leaves a two-parent merge
    whose second parent must be a commit on the base branch (#2177). The
    operator repository's own ``origin/<base>`` may be arbitrarily stale, and a
    stale answer here would refuse a merge that is in fact a base merge — so
    the base is fetched from origin into a kew-owned probe ref for this one
    read, and the ref is dropped again afterwards.

    A base that cannot be fetched, or a reachability read that fails, is
    raised: "not on the base" is only ever returned when git answered. What
    counts as "cannot be fetched" is the probe ref, not ``git fetch``'s verdict
    on the whole command: git also reports work this read never asked for, and
    an unrelated failure in the operator's repository must not refuse a
    legitimate hand-resolved base merge.
    """
    operator = repo_root.resolve()
    probe_ref = f"refs/kew/attest-base/{uuid4().hex}"
    try:
        fetched = _fetch_operator_base(operator, base_branch, probe_ref)
        if fetched.returncode != 0 and not _probe_ref_holds_a_commit(
            operator, probe_ref
        ):
            raise RuntimeError(
                f"the base branch {base_branch!r} could not be fetched from "
                "origin: " + _git_failure_detail(fetched)
            )
        reached = run(
            _checkout_read_command(
                operator, "merge-base", "--is-ancestor", commit_sha, probe_ref
            ),
            check=False,
            env=_checkout_read_env(),
        )
        if reached.returncode not in (0, 1):
            raise RuntimeError(
                f"whether {commit_sha} is on {base_branch!r} could not be read: "
                + _git_failure_detail(reached)
            )
        return reached.returncode == 0
    finally:
        # Pinned exactly like the read above: with ambient GIT_DIR inherited a
        # bare "git update-ref -d" would delete a ref in another repository.
        run(
            _checkout_read_command(operator, "update-ref", "-d", probe_ref),
            check=False,
            env=_checkout_read_env(),
        )


def _probe_ref_holds_a_commit(operator: Path, probe_ref: str) -> bool:
    """Whether the probe fetch actually landed the base in *probe_ref*.

    The one thing :func:`operator_commit_on_base` needs from the fetch, read
    back from the ref instead of inferred from an exit status that covers the
    whole command. *probe_ref* is freshly named per call and dropped again, so a
    ref that resolves here was written by this fetch and nothing else — there is
    no stale answer for this read to mistake for a fresh one.
    """
    resolved = run(
        _checkout_read_command(
            operator, "rev-parse", "--verify", "--quiet", f"{probe_ref}^{{commit}}"
        ),
        check=False,
        env=_checkout_read_env(),
    )
    if resolved.returncode != 0:
        return False
    return _GIT_OID.fullmatch((resolved.stdout or "").strip()) is not None


def _fetch_operator_base(
    operator: Path, base_branch: str, probe_ref: str
) -> subprocess.CompletedProcess[str]:
    """Fetch *base_branch* from the operator repository's origin into *probe_ref*.

    The one network read in :func:`operator_commit_on_base`, and the only git
    call in it that reaches outside the operator repository — so it is pinned
    the same way its paired reachability read is. ``-C`` names the repository
    and ``trusted_git_env`` drops every inherited ``GIT_*``: ``GIT_DIR`` beats
    ``-C``, and ``GIT_CONFIG_*`` is the channel kew carries host git config and
    its own publication token on, so either one inherited would point "the
    freshly fetched base" at a repository the operator never configured — and
    the attestation would then bind a merge of something else entirely.

    The operator's *own* global configuration stays, unlike
    :func:`_checkout_read_env`: this is a network read against their origin and
    the credential helper a private origin needs lives there. That is also why
    the call needs the same guards as the module's other operator-origin read:
    ``GIT_TERMINAL_PROMPT=0`` turns a missing credential into a failure instead
    of a prompt that blocks forever, ``SSH_ASKPASS`` goes because git runs an
    askpass program BEFORE that guard applies, and the deadline bounds a fetch
    that hangs against an unreachable origin.
    """
    env = trusted_git_env(GIT_TERMINAL_PROMPT="0")
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)
    try:
        return run(
            _checkout_read_command(
                operator,
                "fetch",
                "--no-tags",
                # Without it, git ALSO force-updates refs/remotes/origin/<base>
                # from remote.origin.fetch, and a concurrent git process in the
                # operator repository holding that ref's lock fails the whole
                # command even though the probe ref was fetched — refusing a
                # legitimate base merge. This read wants one ref, not a sync.
                "--refmap=",
                "origin",
                f"refs/heads/{base_branch}:{probe_ref}",
            ),
            check=False,
            timeout=_OPERATOR_ORIGIN_QUERY_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"the base branch {base_branch!r} could not be fetched from origin: "
            f"the fetch did not finish within {_OPERATOR_ORIGIN_QUERY_TIMEOUT:.0f}s"
        ) from None


def _checkout_read_command(checkout_dir: Path, *args: str) -> list[str]:
    """Build read-only Git plumbing with executable checkout features disabled.

    A partial-clone fetch is forbidden by the environment the caller passes
    (:func:`checkout_no_fetch_env`), not by ``--no-lazy-fetch``: git before
    2.44 rejects that option, so every read would fail on such a host.
    """
    return [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(checkout_dir),
        *args,
    ]


def _checkout_read_env() -> dict[str, str]:
    """Remove ambient Git redirects and disable system configuration."""
    return checkout_no_fetch_env(
        trusted_git_env(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
        )
    )


#: A filter driver key in ``git config --list --name-only`` output; the
#: subsection is the driver name, which the checkout's attributes choose.
_FILTER_DRIVER_KEY = re.compile(r"filter\.(.+)\.[^.]+", re.DOTALL)
#: A diff driver key naming a program: a textconv, or an external diff.
_DIFF_PROGRAM_KEY = re.compile(r"diff\..+\.(?:textconv|command)", re.DOTALL)


def checkout_no_fetch_env(base: Mapping[str, str]) -> dict[str, str]:
    """*base*, with git forbidden to fetch a missing object.

    A checkout whose config declares a partial clone (``extensions.partialClone``
    and a promisor remote) makes any read that meets a missing object fetch it
    from that remote, which runs the transport the same config names
    (``core.sshCommand``, a ``remote-<scheme>`` helper). An agent writes that
    config, so a host read of its checkout never fetches; a missing object is
    an error instead (#3499).

    ``GIT_NO_LAZY_FETCH`` stops the fetch on git 2.44 and later; older git
    ignores it, so an empty ``GIT_ALLOW_PROTOCOL`` refuses every transport
    as well. Unlike ``-c protocol.allow=never``, which the checkout's own
    ``protocol.<name>.allow`` overrides, the variable beats all config.
    """
    env = dict(base)
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_ALLOW_PROTOCOL"] = ""
    return env


def checkout_content_read_env(
    checkout_dir: Path, base: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """:func:`_checkout_read_env` for a read that hashes working-tree files.

    ``git diff`` or ``git status`` against the working tree hashes any file
    whose stat data the index cannot vouch for, and hashing runs the clean
    filter (or filter process) its attributes name. The driver's name comes
    from attributes the checkout writes (``.gitattributes``,
    ``info/attributes``), so no fixed ``-c`` key can switch it off: every
    driver the checkout's config defines, included files counted, is listed
    and emptied at command scope, which beats every config file, and made
    optional so an emptied driver reads the file as it is. Diff textconv and
    external diff programs are emptied the same way. Use with
    :func:`_checkout_read_command`, which turns off hooks and fsmonitor.

    ``base`` is the environment to extend (the caller's own, when the read
    must see the operator's global configuration); without one, the sanitized
    read environment. ``None`` when the configuration cannot be listed: the
    caller cannot then know which drivers to switch off and must not run the
    read. The listing itself, and the read the caller runs, never fetch: see
    :func:`checkout_no_fetch_env`.
    """
    env = _checkout_read_env() if base is None else checkout_no_fetch_env(base)
    try:
        listed = run(
            _checkout_read_command(
                checkout_dir, "config", "--list", "--name-only", "-z"
            ),
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        _logger.warning(
            "cannot list %s's git configuration: %s", checkout_dir, type(exc).__name__
        )
        return None
    if listed.returncode != 0:
        return None
    _append_git_config(env, "diff.external", "")
    drivers: set[str] = set()
    for name in (listed.stdout or "").split("\0"):
        filter_key = _FILTER_DRIVER_KEY.fullmatch(name)
        if filter_key is not None:
            drivers.add(filter_key.group(1))
        elif _DIFF_PROGRAM_KEY.fullmatch(name):
            _append_git_config(env, name, "")
    for driver in sorted(drivers):
        for key in ("clean", "smudge", "process"):
            _append_git_config(env, f"filter.{driver}.{key}", "")
        _append_git_config(env, f"filter.{driver}.required", "false")
    return env


#: Command-scope configuration (above the checkout's own ``.git/config``) that
#: pins which objects git reads and stops it running checkout-supplied programs.
_CHECKOUT_OBJECT_CONFIG = (
    ("core.useReplaceRefs", "false"),
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    # GIT_GRAFT_FILE=/dev/null exists, so git prints a ~300-character
    # deprecation hint first on stderr; the push-failure handlers keep only
    # the head of stderr, which would lose the real error and the
    # force-with-lease "stale info" signal.
    ("advice.graftFileDeprecated", "false"),
)


def checkout_object_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Git environment that reads an agent checkout's real objects.

    Used for the pre-push scan and for the push itself, so what is scanned is
    what is sent. ``base`` is the push's own environment (its auth); without
    one, the sanitized read environment. Replace refs and grafts are ignored
    whatever the checkout says; hooks and fsmonitor never run.
    """
    env = _checkout_read_env() if base is None else dict(base)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull
    for key, value in _CHECKOUT_OBJECT_CONFIG:
        _append_git_config(env, key, value)
    return env


def unpushed_commit_count(checkout_dir: Path, repo_root: Path, branch_name: str) -> int:
    """Count commits beyond the operator origin's authoritative branch head.

    Never consult the agent checkout's remote configuration. Query the exact
    branch through the operator repository. Then inspect only local objects in
    the checkout under a sanitized environment.
    """
    remote_head = _operator_remote_branch_head(repo_root, branch_name)
    read_env = _checkout_read_env()
    exists = run(
        _checkout_read_command(
            checkout_dir, "cat-file", "-e", f"{remote_head}^{{commit}}"
        ),
        check=False,
        env=read_env,
    )
    if exists.returncode != 0:
        detail = _git_failure_detail(exists)
        raise RuntimeError(f"operator origin head is absent from checkout: {detail}")

    reachable = run(
        _checkout_read_command(
            checkout_dir, "merge-base", "--is-ancestor", remote_head, "HEAD"
        ),
        check=False,
        env=read_env,
    )
    if reachable.returncode != 0:
        raise RuntimeError(
            "operator origin head is not an ancestor of checkout HEAD: "
            f"{_git_failure_detail(reachable)}"
        )

    counted = run(
        _checkout_read_command(
            checkout_dir, "rev-list", "--count", f"{remote_head}..HEAD"
        ),
        check=False,
        env=read_env,
    )
    value = (counted.stdout or "").strip()
    if counted.returncode != 0 or not value.isdigit():
        raise RuntimeError(
            f"checkout commit count failed: {_git_failure_detail(counted)}"
        )
    return int(value)


def _quarantine_checkout(checkout_dir: Path) -> Path:
    """Atomically preserve a replaced checkout beside its original path."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine = checkout_dir.with_name(
        f"{checkout_dir.name}.replaced-{timestamp}-{uuid4().hex[:12]}"
    )
    revoke_checkout_generation(checkout_dir)
    try:
        checkout_dir.rename(quarantine)
    except OSError as exc:
        raise RuntimeError(
            f"could not preserve checkout at {checkout_dir} before replacement: {exc}"
        ) from exc
    _warn(f"Preserved replaced checkout at {quarantine}.")
    return quarantine


def _clone_checkout_on(
    checkout_dir: Path,
    branch_name: str,
    *,
    start_point: str,
    exact_sha: str = "",
    fetch_exact_sha: bool = False,
) -> None:
    """Clone an isolated checkout and put *branch_name* on ``origin/<start_point>``.

    Shared by the create-new-branch (:func:`setup_worktree`) and
    check-out-existing (:func:`_create_fresh_worktree`) paths, which differ
    only in which ref the branch starts from.

    With *exact_sha* the branch starts at that commit, not at whatever
    ``origin/<start_point>`` resolves to after the fetch: the base kew recorded
    for publication and the base the agent edits on are then one commit even
    when the remote advances in between (review round 13 of #1440). A commit
    the fetch did not bring is a refusal.

    With *fetch_exact_sha*, fetch that SHA directly. The caller must verify the
    remote branch still equals it before launching the agent.
    """
    if checkout_dir.exists():
        # `git clone` refuses a non-empty target, and the caller's removal can
        # fail — `git worktree remove` does nothing for an existing clone.
        _quarantine_checkout(checkout_dir)
    # A token a crashed removal left beside an absent path must not outlive
    # it into the clone that takes the path (review round 18).
    revoke_checkout_generation(checkout_dir)
    try:
        repo_root = _repo_root()
        bootstrap_ref = exact_sha if fetch_exact_sha else start_point
        clone_local_checkout(repo_root, checkout_dir, bootstrap_ref)
        fetch_ref(
            checkout_dir,
            exact_sha if fetch_exact_sha else start_point,
            repo_root=repo_root,
        )
        target = f"origin/{start_point}"
        if exact_sha:
            if not fetch_exact_sha:
                require_base_fetched(checkout_dir, exact_sha, start_point)
            target = exact_sha
        # -B keeps this deterministic if a prior transport ever retains the
        # branch name. The checkout must land on the freshly fetched ref, never
        # on incidental clone state.
        run(
            ["git", "checkout", "-B", branch_name, target, "--quiet"],
            cwd=checkout_dir,
            check=True,
        )
        write_checkout_generation(checkout_dir)
    except BaseException:
        # A half-built clone must not survive: the next call takes the reuse
        # path into it and works from a tree nobody assembled.
        remove_checkout_tree(checkout_dir)
        raise


def require_base_fetched(checkout_dir: Path, base_sha: str, base_branch: str) -> None:
    """Refuse a trusted base that the freshly fetched ``origin/<base_branch>``
    does not descend from (review round 14 of #1440).

    Presence is not enough: the operator's local repository can still hold a
    base the remote has rewritten away, and a checkout cut there would start
    on divergent history and publish its rollback as agent work. Runs in a
    host-created clone before any agent touches it.
    """
    reachable = run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            base_sha,
            f"origin/{base_branch}",
        ],
        cwd=checkout_dir,
        check=False,
    )
    if reachable.returncode != 0:
        raise RuntimeError(
            f"the trusted base {base_sha[:12]} is not reachable from "
            f"origin/{base_branch} as fetched; the remote moved away from it, "
            "retry the dispatch"
        )


def checkout_existing_worktree(
    *,
    issue_num: int,
    branch_name: str,
    config: DispatchConfig,
    base_branch: str = "",
    expected_head_sha: str = "",
    pinned_head_sha: str | None = None,
    item_id: str | None = None,
    provider: str = "github",
) -> tuple[Path, ConflictInfo | None]:
    """Check out an existing remote branch into a worktree.

    Unlike setup_worktree() which creates a new branch from origin/base,
    this fetches an existing branch (e.g., from a previous dispatch PR)
    and creates a worktree from it.

    If the worktree directory already exists (preserved from a failed
    dispatch), it is reused after fetching the latest changes.

    Once the worktree is materialized — on **both** the reused and the
    freshly-created path — the configured resolve strategy (merge or rebase)
    integrates *base_branch* when it is provided. A returned ``ConflictInfo``
    therefore means "base genuinely conflicts with this branch"; ``None`` means
    "base integrated with zero conflicts". Previously the fresh-worktree path
    returned ``(dir, None)`` unconditionally without ever touching base, so a
    PR that conflicts with base was silently reported as clean (#790).

    Args:
        issue_num: The issue number (for directory naming).
        branch_name: The remote branch name to check out.
        config: Dispatch configuration.
        base_branch: The base branch to integrate (e.g. ``"main"``).
        expected_head_sha: The recorded PR head that reused work must descend from.
        pinned_head_sha: Materialize exactly this validated SHA in a fresh clone.
            Refuse local or remote head drift before integrating the base.
        item_id: Opaque work-item id (A5a); when given the worktree directory
            derives from ``branch_slug(item_id)`` instead of *issue_num*.
        provider: Tracker provider used to validate *item_id*.

    Returns:
        Tuple of (worktree path, ConflictInfo or None).
    """
    worktree_dir = dispatch_checkout_path(
        config, issue_num=issue_num, item_id=item_id, provider=provider
    )
    claim_checkout(worktree_dir)

    if pinned_head_sha is not None:
        conflict = _checkout_pinned_pr_head(
            worktree_dir,
            branch_name,
            pinned_head_sha,
            base_branch=base_branch,
            strategy=config.resolve.strategy,
        )
        return worktree_dir, conflict

    # Always fetch to get latest changes
    run(["git", "fetch", "origin", branch_name, "--quiet"])  # check=True is default
    if base_branch:
        run(["git", "fetch", "origin", base_branch, "--quiet"])

    if not _reuse_existing_worktree(
        worktree_dir, branch_name, base_branch, config=config
    ):
        _create_fresh_worktree(worktree_dir, branch_name)
        if base_branch:
            fetch_ref(worktree_dir, base_branch)

    if expected_head_sha:
        lineage = run(
            [
                "git",
                "-C",
                str(worktree_dir),
                "merge-base",
                "--is-ancestor",
                expected_head_sha,
                "HEAD",
            ],
            check=False,
        )
        if lineage.returncode != 0:
            raise RuntimeError(
                f"checkout at {worktree_dir} does not descend from the recorded "
                f"PR head {expected_head_sha[:12]}"
            )

    # Integrate the (possibly moved) base into the materialized worktree so a
    # PR that conflicts with base is detected on every path — the fresh-worktree
    # case included. "Merged cleanly" (None) now means base integrated with zero
    # conflicts, not "we never looked".
    if base_branch:
        conflict = _apply_resolve_strategy(
            worktree_dir=worktree_dir,
            base_branch=base_branch,
            strategy=config.resolve.strategy,
        )
        return worktree_dir, conflict
    return worktree_dir, None


def _checkout_pinned_pr_head(
    worktree_dir: Path,
    branch_name: str,
    head_sha: str,
    *,
    base_branch: str = "",
    strategy: str = "merge",
) -> ConflictInfo | None:
    """Materialize the validated head and refuse drift before base integration."""
    if not _GIT_OID.fullmatch(head_sha):
        raise ValueError("PR checkout requires a full validated PR head SHA")
    _clone_checkout_on(
        worktree_dir,
        branch_name,
        start_point=branch_name,
        exact_sha=head_sha,
        fetch_exact_sha=True,
    )
    if base_branch:
        fetch_ref(worktree_dir, base_branch)
    require_pinned_pr_head(worktree_dir, branch_name, head_sha)
    if base_branch:
        return _apply_resolve_strategy(
            worktree_dir=worktree_dir,
            base_branch=base_branch,
            strategy=strategy,
        )
    return None


def _checkout_head_via_transport(checkout_dir: Path) -> str:
    """Read a checkout's HEAD through the file transport from a scratch directory.

    Host Git never runs inside an agent checkout, read or write (#1440
    criterion 1); ``git ls-remote <checkout> HEAD`` answers from outside it,
    the same way canonical publication reads agent commits.
    """
    with TemporaryDirectory(prefix="kew-checkout-head-") as query_dir:
        listed = run(
            ["git", "ls-remote", str(checkout_dir), "HEAD"],
            cwd=Path(query_dir),
            # The read environment refuses every transport; this read IS one.
            env={**_checkout_read_env(), "GIT_ALLOW_PROTOCOL": "file"},
            check=False,
        )
    if listed.returncode != 0:
        raise RuntimeError(
            "cannot run: PR checkout HEAD could not be read: "
            + _git_failure_detail(listed)
        )
    for line in (listed.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref == "HEAD" and _GIT_OID.fullmatch(sha):
            return sha
    raise RuntimeError("cannot run: PR checkout HEAD could not be read")


def require_pinned_pr_head(worktree_dir: Path, branch_name: str, head_sha: str) -> None:
    """Require exact checkout and remote heads before integration or agent launch."""
    actual_head = _checkout_head_via_transport(worktree_dir)
    if actual_head != head_sha:
        raise RuntimeError(
            f"cannot run: PR checkout HEAD {actual_head} "
            f"differs from validated SHA {head_sha}"
        )
    remote_head = _operator_remote_branch_head(_repo_root(), branch_name)
    if remote_head != head_sha:
        raise RuntimeError(
            f"cannot run: PR branch {branch_name!r} moved from validated SHA "
            f"{head_sha} "
            f"to {remote_head} before launch"
        )


def _reuse_existing_worktree(
    worktree_dir: Path,
    branch_name: str,
    base_branch: str,
    *,
    config: DispatchConfig | None = None,
) -> bool:
    """Reuse a preserved worktree when it exists and is on the expected branch.

    Returns ``True`` when the worktree was materialized in place (the caller
    should integrate base into it); ``False`` when no usable worktree exists and
    the caller must create one fresh.

    A branch that diverged from its remote (``git pull --ff-only`` fails) is
    still reused: with *base_branch* provided, the base integration that follows
    reconciles it; with no base to integrate, there is nothing left to try, so
    the divergence is a hard error.
    """
    if not worktree_dir.exists():
        return False
    if config is not None and _canonical_never_reuses(worktree_dir, config):
        return False

    if _replace_legacy_linked_worktree(worktree_dir):
        return False

    branch_check = run(
        ["git", "-C", str(worktree_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
    )
    current_branch = branch_check.stdout.strip() if branch_check.returncode == 0 else ""

    if current_branch != branch_name:
        _warn(
            f"Worktree at {worktree_dir} is on branch '{current_branch}', "
            f"expected '{branch_name}'. Recreating."
        )
        run(
            ["git", "worktree", "remove", str(worktree_dir), "--force"],
            check=False,
        )
        return False

    pull_result = run(
        ["git", "-C", str(worktree_dir), "pull", "--ff-only", "--quiet"],
        check=False,
    )
    if pull_result.returncode != 0 and not base_branch:
        raise RuntimeError(
            f"git pull --ff-only failed in {worktree_dir} for branch "
            f"{branch_name} (exit {pull_result.returncode})"
        )
    return True


def _replace_legacy_linked_worktree(worktree_dir: Path) -> bool:
    """Replace a clean linked worktree, or refuse one with pending work."""
    if not is_linked_worktree(worktree_dir):
        return False
    if has_uncommitted_changes(worktree_dir):
        raise RuntimeError(
            f"{worktree_dir} is a legacy linked worktree sharing the "
            "operator's .git and has uncommitted changes. Commit, stash, "
            "or remove it; kew will then replace it with an isolated clone."
        )
    _warn(
        f"Replacing legacy linked worktree at {worktree_dir} with an "
        "isolated clone (it shared the operator's .git)."
    )
    discard_checkout(worktree_dir, release_claim=False)
    return True


def _canonical_never_reuses(worktree_dir: Path, config: DispatchConfig) -> bool:
    """Canonical mode never runs git in an existing agent checkout: a ``.git``
    file (a linked worktree, or a pointer an agent wrote) is refused from its
    mode alone, anything else is moved aside intact (review round 11). False
    only on the escape hatch, whose git-based reuse follows."""
    if config.publication.publish != "canonical":
        return False
    if is_linked_worktree(worktree_dir):
        raise RuntimeError(
            f"{worktree_dir} carries a .git file (a linked worktree or an "
            "agent-written pointer); canonical publication runs no git inside "
            "it. Move or remove the directory and retry"
        )
    quarantine_agent_checkout(
        worktree_dir, config, reason="a previous run left this checkout"
    )
    return True


def is_linked_worktree(path: Path) -> bool:
    """True when *path* is a linked worktree sharing another repository's ``.git``.

    A linked worktree's ``.git`` is a FILE (``gitdir: <elsewhere>``); a
    standalone clone's is a directory. This is the upgrade check: an operator
    who updates kew keeps the checkouts they already have, and those were
    created as linked worktrees. Without detecting them, the containment fix
    would apply only to checkouts created after the upgrade — which is the
    quiet half-fix that reads as done.
    """
    mode = _lstat_mode(path / ".git")
    return mode is not None and stat.S_ISREG(mode)


def _lstat_mode(path: Path) -> int | None:
    """Return *path*'s own mode without following links."""
    if not os.path.lexists(path):
        return None
    try:
        return path.lstat().st_mode
    except OSError as exc:
        _warn(f"Could not inspect checkout metadata at {path}: {exc}")
        return None


def _has_own_git_metadata(dot_git: Path) -> bool:
    """True when HEAD and objects belong directly to this ``.git`` directory."""
    head_mode = _lstat_mode(dot_git / "HEAD")
    objects_mode = _lstat_mode(dot_git / "objects")
    return (
        head_mode is not None
        and stat.S_ISREG(head_mode)
        and objects_mode is not None
        and stat.S_ISDIR(objects_mode)
    )


def _alternates_stay_inside_checkout(path: Path, dot_git: Path) -> bool:
    """Reject an alternate object store that resolves outside *path*."""
    alternates = dot_git / "objects" / "info" / "alternates"
    if not os.path.lexists(alternates):
        return True
    mode = _lstat_mode(alternates)
    checkout_root = path.resolve()
    if (
        mode is None
        or not stat.S_ISREG(mode)
        or not alternates.resolve().is_relative_to(checkout_root)
    ):
        return False
    try:
        entries = alternates.read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        _warn(f"Could not inspect git object alternates at {alternates}: {exc}")
        return False
    base = alternates.parent
    for entry in entries:
        raw_path = Path(entry.strip())
        if not entry.strip():
            continue
        alternate = raw_path if raw_path.is_absolute() else base / raw_path
        if not alternate.resolve().is_relative_to(checkout_root):
            return False
    return True


def is_safe_standalone_clone(path: Path) -> bool:
    """True only for a clone whose git metadata is contained in *path*."""
    path_mode = _lstat_mode(path)
    dot_git = path / ".git"
    dot_git_mode = _lstat_mode(dot_git)
    if (
        path_mode is None
        or not stat.S_ISDIR(path_mode)
        or dot_git_mode is None
        or not stat.S_ISDIR(dot_git_mode)
    ):
        return False
    if not _has_own_git_metadata(dot_git):
        return False
    if os.path.lexists(dot_git / "commondir"):
        return False
    return _alternates_stay_inside_checkout(path, dot_git)


def linked_worktree_resume_message(path: Path) -> str:
    """Migration guidance for a linked checkout that cannot be resumed."""
    return (
        f"refusing to resume linked worktree at {path}; push the branch or copy "
        "the directory, then remove it and resume"
    )


def unsafe_clone_resume_message(path: Path) -> str:
    """Guidance for a checkout that is not a contained standalone clone."""
    return (
        f"refusing to resume unsafe checkout at {path}; expected a standalone "
        "clone with contained git metadata. Copy any needed work elsewhere, "
        "then remove the checkout and restore it from its pushed branch"
    )


def discard_checkout(checkout_dir: Path, *, release_claim: bool = True) -> None:
    """Remove *checkout_dir* whether it is a linked worktree or a clone.

    ``git worktree remove`` is addressed at the operator's repository
    explicitly rather than relying on the process working directory, and it is
    a no-op for a clone — which is why the ``rmtree`` follows unconditionally.
    ``prune`` then clears the administrative entry that a failed removal would
    otherwise leave dangling in the operator's ``.git/worktrees``.
    """
    # The identity goes first: a removal interrupted after it leaves a
    # directory nothing can bind, never a token beside an absent path that a
    # successor could inherit (review round 18).
    revoke_checkout_generation(checkout_dir)
    root = _repo_root_or_none()
    if root is not None:
        run(
            [
                "git",
                "-C",
                str(root),
                "worktree",
                "remove",
                str(checkout_dir),
                "--force",
            ],
            check=False,
        )
    shutil.rmtree(checkout_dir, ignore_errors=True)
    if root is not None:
        run(["git", "-C", str(root), "worktree", "prune"], check=False)
    if release_claim:
        release_checkout_claim(checkout_dir)


def _create_fresh_worktree(worktree_dir: Path, branch_name: str) -> None:
    """Clone a standalone checkout and place it on ``origin/<branch_name>``.

    A CLONE, not ``git worktree add`` (W1.1 / D2, doors 2-4). A linked
    worktree's ``.git`` points into the operator's repository, so an agent
    working here writes the operator's config and hooks — traps that fire on
    the operator's own interactive git commands, with no kew dispatch involved.

    Deleting the stale local branch is gone with it: the branch now lives in
    the clone, so there is no longer a destructive ``git branch -D`` taking an
    agent-influenced name against the operator's repository (#1445).
    """
    _clone_checkout_on(worktree_dir, branch_name, start_point=branch_name)


def _repo_root() -> Path:
    """The repository agent checkouts are cloned from — the operator's own."""
    root = _repo_root_or_none()
    if root is None:
        raise RuntimeError("cannot locate the repository root to clone from")
    return root


def operator_repo_root() -> Path | None:
    """The operator's own checkout, or ``None`` outside a repository.

    Public seam for host-side reads that must land on a root the dispatched
    run cannot write. Agent checkouts are clones OF this root, materialized on
    a retry or a continuation from the prior attempt's branch, so a file read
    from one is agent-authored content and not a deployed artifact — which is
    why the pre-publish self-check resolves the reviewer rubric through here
    (#2454).

    Resolved by walking up from the process working directory, and
    deliberately NOT by a ``git rev-parse`` subprocess. This is read while the
    dispatch prompt is being built, which is before the launch has bound the
    checkout it is about to run: a forged checkout must refuse before ANY git
    process starts (round 26 of #1440), and ``kew prompt`` previews have no
    executable allowlist to launch git with at all. Outside a repository the
    answer is ``None``, which ships the packaged artifact — a self-check
    quoting an untuned rubric, not an absent self-check.
    """
    try:
        start = Path.cwd()
    except OSError as exc:
        _warn(
            "cannot locate the operator repository root "
            f"({exception_text(exc)}); prompts fall back to packaged artifacts"
        )
        return None
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _repo_root_or_none() -> Path | None:
    """:func:`_repo_root` for cleanup paths, which must not raise."""
    result = run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        env=trusted_git_env(),
    )
    root = result.stdout.strip() if result.returncode == 0 else ""
    return Path(root) if root else None


def integrate_base_branch(
    *,
    worktree_dir: Path,
    base_branch: str,
    strategy: str,
) -> ConflictInfo | None:
    """Public seam over :func:`_apply_resolve_strategy`.

    Callers outside this module (the review checkout, #1290) materialize their
    own tree and then need the SAME base integration the dispatch worktrees get
    — one implementation, not a second copy that can drift.
    """
    return _apply_resolve_strategy(
        worktree_dir=worktree_dir,
        base_branch=base_branch,
        strategy=strategy,
    )


_MAX_GIT_DETAIL_CHARS = 500


def _truncate_git_detail(text: str) -> str:
    """Keep git's own diagnosis, bounded — it lands in a status_reason."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_GIT_DETAIL_CHARS:
        return collapsed
    return collapsed[: _MAX_GIT_DETAIL_CHARS - 1] + "…"


def _fetched_base_sha(worktree_dir: Path, base_branch: str) -> str:
    """Return the fetched base commit or fail before agent dispatch."""
    result = run(
        [
            "git",
            "-C",
            str(worktree_dir),
            "rev-parse",
            "--verify",
            f"origin/{base_branch}^{{commit}}",
        ],
        check=False,
    )
    base_sha = result.stdout.strip() if result.returncode == 0 else ""
    if not base_sha:
        raise BaseIntegrationError(
            base_branch, "cannot resolve the fetched base commit"
        )
    return base_sha


def _apply_resolve_strategy(
    *,
    worktree_dir: Path,
    base_branch: str,
    strategy: str,
) -> ConflictInfo | None:
    """Apply merge or rebase strategy to integrate base branch changes.

    Returns ConflictInfo if conflicts remain, None if resolved cleanly.
    """
    if strategy == "rebase":
        rebase_result = run(
            ["git", "-C", str(worktree_dir), "rebase", f"origin/{base_branch}"],
            check=False,
        )
        if rebase_result.returncode == 0:
            return None
        # Rebase failed — abort and fall back to merge
        run(
            ["git", "-C", str(worktree_dir), "rebase", "--abort"],
            check=False,
        )

    # Merge strategy (or rebase fallback)
    merge_result = run(
        ["git", "-C", str(worktree_dir), "merge", f"origin/{base_branch}"],
        check=False,
    )
    if merge_result.returncode == 0:
        return None

    # Non-zero merge: conflicts, or a merge that could not run at all. The two
    # are distinguishable without new machinery — git cannot report conflicts
    # with no unmerged paths — so ask, instead of assuming (#1455).
    diff_result = run(
        ["git", "-C", str(worktree_dir), "diff", "--name-only", "--diff-filter=U"],
        check=False,
    )
    conflicted = [f for f in diff_result.stdout.strip().split("\n") if f]
    if not conflicted:
        detail = (merge_result.stderr or merge_result.stdout or "").strip()
        raise BaseIntegrationError(
            base_branch,
            _truncate_git_detail(detail)
            or f"git merge exited {merge_result.returncode} with no output",
        )
    return ConflictInfo(
        strategy_used="merge",
        conflicted_files=conflicted,
        base_branch=base_branch,
        base_sha=_fetched_base_sha(worktree_dir, base_branch),
    )


def reuse_or_restore_worktree(
    *,
    issue_num: int,
    config: DispatchConfig,
    item_id: str | None = None,
    provider: str = "github",
    expected_generation: str = "",
    on_restore: Callable[[Path], None] | None = None,
    replace_existing: bool = True,
) -> Path:
    """Return the loop's EXISTING worktree, or restore it from the pushed branch.

    Unlike ``setup_worktree`` (which force-removes the worktree + branch and
    recreates from base), this preserves prior work — it is for resuming a loop.
    Mirrors ``checkout_existing_worktree``'s branch-verify + prune discipline.

    Reuse: the worktree dir exists AND HEAD is on the loop's branch -> reuse
    as-is (its local commits ARE the work; no fetch needed, so a not-yet-pushed
    loop is still resumable). A wrong-branch / detached-HEAD dir is removed and
    restored. Restore: recreate from the pushed branch ``origin/<branch>``.
    Fail-closed: no usable worktree AND no pushed branch -> raise (never resume
    against base/empty, which would discard the prior work and could let a fresh
    agent "achieve" a clean tree).

    *item_id* threads the opaque work-item id (A5a); when given, the branch
    name and worktree directory derive from ``branch_slug(item_id)``.
    *expected_generation* refuses a replaced existing checkout before Git.
    *on_restore* durably binds the fresh host generation before it is returned.
    *replace_existing* False refuses, before any Git write, to remove an
    existing checkout that sits on another branch: its commits may exist
    nowhere else, and a caller whose recorded binding cannot be rebound would
    otherwise destroy them and only then discover it cannot continue.
    The calling thread must hold the checkout claim through the resumed session.
    """
    slug = _item_slug(issue_num, item_id, provider)
    branch_name = f"{config.branches.prefix}{slug}"
    worktree_dir = dispatch_checkout_path(
        config, issue_num=issue_num, item_id=item_id, provider=provider
    )
    require_checkout_claim(worktree_dir)
    if not expected_generation:
        raise RuntimeError(
            "loop has no recorded checkout generation; start a fresh loop with "
            "kew dispatch <issue> --loop"
        )
    if worktree_dir.exists():
        hold_checkout_generation(worktree_dir, expected_generation)
        if not is_safe_standalone_clone(worktree_dir):
            # This checkout may hold dirty files or commits that exist nowhere
            # else. Refusal must not mutate it.
            message = (
                linked_worktree_resume_message(worktree_dir)
                if is_linked_worktree(worktree_dir)
                else unsafe_clone_resume_message(worktree_dir)
            )
            raise RuntimeError(message)
        head = run(
            ["git", "-C", str(worktree_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
            capture=True,
        )
        if head.returncode == 0 and head.stdout.strip() == branch_name:
            return worktree_dir
        if not replace_existing:
            raise RuntimeError(
                f"loop checkout at {worktree_dir} is not on {branch_name}; its "
                "commits may exist nowhere else and its recorded checkout "
                "binding cannot be rebound, so the checkout is preserved; "
                "start a fresh loop with kew dispatch <issue> --loop"
            )
        # Wrong branch / detached HEAD -> remove and restore from origin. The
        # lease pins THIS directory; the restore makes another one.
        release_checkout_binding(worktree_dir)
        run(
            ["git", "worktree", "remove", str(worktree_dir), "--force"],
            check=False,
            capture=True,
        )
    restored = _restore_worktree(worktree_dir, branch_name)
    # The restore made a NEW directory. The sidecar beside it still names the
    # one that is gone, so every read of the restored checkout would refuse and
    # the rebind could never happen (governed review round 5, 2026-09-21). Mint
    # fresh evidence under the claim the caller already holds.
    revoke_checkout_generation(restored)
    write_checkout_generation(restored)
    if on_restore is not None:
        on_restore(restored)
    return restored


def _restore_worktree(worktree_dir: Path, branch_name: str) -> Path:
    """Recreate a worktree from the pushed branch (fail-closed if absent)."""
    require_checkout_claim(worktree_dir)
    if not remote_branch_exists(branch_name):
        raise RuntimeError(
            f"cannot restore worktree for branch {branch_name!r}: no usable local "
            "worktree and no pushed branch on origin"
        )
    run(["git", "fetch", "origin", branch_name, "--quiet"], check=False, capture=True)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    # An isolated clone, like every other checkout an agent is handed. The
    # prune + `git branch -D` that used to precede this are gone with the
    # linked worktree: they existed only to clear operator-repo state that
    # `git worktree add` would trip over, and a clone has none.
    try:
        _clone_checkout_on(worktree_dir, branch_name, start_point=branch_name)
    except Exception as exc:
        raise RuntimeError(
            f"failed to restore worktree for branch {branch_name!r}: {exc}"
        ) from exc
    return worktree_dir


def has_commits_ahead_of_base(
    checkout_dir: Path, base_branch: str, *, operator_repo: Path, branch: str = ""
) -> bool:
    """Does the checkout's HEAD carry commits no published branch holds?

    The other half of checkout publication's no-change test: a committed but
    unpublished change leaves a clean tree, which
    :func:`has_uncommitted_changes` alone reads as nothing to lose (#2971).

    The base is never read from the checkout: an agent that commits and then
    runs ``git update-ref refs/remotes/origin/<base> HEAD`` in its own clone
    makes any count against that ref zero. Only HEAD is read there, under
    :func:`checkout_object_env`, so the checkout's replace refs and grafts
    cannot stand in for it; whether that commit is on the base is answered
    by the OPERATOR repository against a freshly fetched base
    (:func:`operator_commit_on_base`). An unpublished commit is an object the
    operator repository does not have, so the answer fails and reads as work.

    The checkout's own item *branch*, once pushed, holds its commits too: a
    retry starts clean at that branch's published head, ahead of the base,
    and nothing there is lost. So a HEAD the base lacks is asked of the item
    branch on the operator's origin as well, fetched the same way; only a
    commit neither holds is work.

    A branch with no commit yet has none to lose; any other answer that
    cannot be read — an unreadable HEAD, a base or item branch that cannot
    be fetched — reads as commits to preserve (safe default).
    """
    env = checkout_object_env()
    head = run(
        _checkout_read_command(checkout_dir, "rev-parse", "-q", "--verify", "HEAD"),
        check=False,
        env=env,
    )
    sha = (head.stdout or "").strip()
    if head.returncode == 1 and not sha:
        return False
    if head.returncode != 0 or _GIT_OID.fullmatch(sha) is None:
        return True
    if _operator_branch_holds(operator_repo, sha, base_branch):
        return False
    if not branch or branch == base_branch:
        return True
    return not _operator_branch_holds(operator_repo, sha, branch, ask_origin_first=True)


def _operator_branch_holds(
    operator_repo: Path, sha: str, branch: str, *, ask_origin_first: bool = False
) -> bool:
    """Is *sha* on *branch* as the operator's origin holds it now?

    Every answer git could not give reads ``False``: a commit the operator
    repository lacks is unpublished, and a branch origin does not have, or
    that cannot be fetched, holds nothing this read can vouch for. An item
    branch is asked of origin first (*ask_origin_first*): a node whose work
    was never pushed has none.
    """
    try:
        if ask_origin_first and not _operator_branch_published(operator_repo, branch):
            return False
        return operator_commit_on_base(operator_repo, sha, branch)
    except RuntimeError as exc:
        _warn(f"{sha} counts as unpublished: {branch!r} on origin: {exc}")
        return False


def has_uncommitted_changes(worktree_dir: Path) -> bool:
    """Check if a worktree has uncommitted changes (staged, unstaged, or untracked).

    Returns True if there are changes or if git status fails (safe default).

    Untracked files are listed unconditionally: ``--untracked-files=all`` and
    the inline ``-c`` override defeat a repository-level
    ``status.showUntrackedFiles=no``, which an agent can set inside its own
    checkout to hide an unpublished file from this check. ``core.fsmonitor`` is
    disabled for the same reason: a monitor hook that reports no changed paths
    lets Git trust its cached index and skip the tracked-file scan, which hides
    a modified tracked file. Ignored paths stay excluded: ignore rules are the
    checkout's own, and venvs and caches are ignored in every checkout.

    An index entry flagged assume-unchanged or skip-worktree is excluded from
    the status scan by design, so a checkout that carries either flag is
    reported dirty outright: an agent can set the flag on a tracked path and
    then edit it, and no status invocation would ever show the edit.
    """
    env = checkout_content_read_env(worktree_dir)
    if env is None:
        return True  # Can't switch off its filter drivers — assume changes exist
    result = run(_hardened_status_command(worktree_dir), check=False, env=env)
    if result.returncode != 0:
        return True  # Can't determine status — assume changes exist
    if result.stdout.strip():
        return True
    if _index_hides_tracked_paths(worktree_dir):
        return True
    return _submodules_dirty(worktree_dir)


def _submodules_dirty(worktree_dir: Path) -> bool:
    """Whether any submodule of the checkout holds uncommitted content.

    The superproject's status summarizes a submodule through
    ``submodule.<name>.ignore``, which the checkout's own config can set to
    ``all``; the same hardened status inside each checked-out submodule sees
    what that summary was told to hide.
    """
    checked_out = _checked_out_submodules(worktree_dir)
    if checked_out is None or _unlisted_submodule_checkout(worktree_dir, checked_out):
        return True
    for path in checked_out:
        inner_env = checkout_content_read_env(worktree_dir / path)
        if inner_env is None:
            return True
        inner = run(
            _hardened_status_command(worktree_dir / path), check=False, env=inner_env
        )
        if inner.returncode != 0 or inner.stdout.strip():
            return True
    return False


def _unlisted_submodule_checkout(worktree_dir: Path, checked_out: list[str]) -> bool:
    """Whether a gitlink holds a repository ``git submodule status`` skipped.

    An inactive submodule (its ``submodule.<name>`` section removed from the
    checkout's config) is not listed as checked out, so its dirt is never read
    — and the superproject's status no longer reads it either (``dirty``). A
    gitlink whose directory still holds a ``.git`` is a repository whose
    content nothing here inspected, so it counts as dirty. The gitlinks of
    every checked-out submodule are walked too, for a nested inactive one.
    Unreadable index state counts as dirty, the safe default.
    """
    listed_paths = set(checked_out)
    for parent in ("", *checked_out):
        listed = run(
            _checkout_read_command(worktree_dir / parent, "ls-files", "-s", "-z"),
            check=False,
            env=_checkout_read_env(),
        )
        if listed.returncode != 0:
            return True
        for entry in listed.stdout.split("\0"):
            meta, _, path = entry.partition("\t")
            if not meta.startswith("160000 "):
                continue
            gitlink = f"{parent}/{path}" if parent else path
            if gitlink not in listed_paths and os.path.lexists(
                worktree_dir / gitlink / ".git"
            ):
                return True
    return False


def _hardened_status_command(directory: Path) -> list[str]:
    """``git status --porcelain`` with every hiding knob forced off.

    Built on :func:`_checkout_read_command`, so hooks and fsmonitor are off
    as for every other read of an agent checkout; the status-specific knobs
    (untracked files, submodule summaries) are forced here, and the
    opportunistic index refresh is off (``--no-optional-locks``): a ``.git``
    file can name another repository, whose index a read must never rewrite.
    A submodule's
    recorded commit is compared, but not its dirt (``dirty``): git reads that
    in a child process under the submodule's own config, which no switch here
    reaches, so the caller runs this command inside each submodule instead.
    """
    return _checkout_read_command(
        directory,
        "--no-optional-locks",
        "-c",
        "status.showUntrackedFiles=all",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=dirty",
    )


def _index_hides_tracked_paths(worktree_dir: Path) -> bool:
    """Whether any index entry is flagged assume-unchanged (lowercase tag) or
    skip-worktree (``S``), which ``git status`` silently skips. Every
    initialized submodule is inspected too: a flag inside a submodule index
    hides the edit from the submodule's own status, and the superproject then
    reports its gitlink as unchanged. Unreadable index state counts as hidden,
    the safe default."""
    listings = (
        _checkout_read_command(worktree_dir, "ls-files", "-v", "-z"),
        _checkout_read_command(
            worktree_dir,
            "submodule",
            "--quiet",
            "foreach",
            "--recursive",
            "git -c core.fsmonitor=false ls-files -v -z",
        ),
    )
    for argv in listings:
        listed = run(argv, check=False, env=_checkout_read_env())
        if listed.returncode != 0:
            return True
        if _any_flagged_index_entry(listed.stdout):
            return True
    return _retained_submodule_state(worktree_dir)


def _retained_submodule_state(worktree_dir: Path) -> bool:
    """Whether a submodule without a checked-out worktree still has a module.

    ``git submodule foreach`` skips a submodule that is not checked out, and
    the superproject's status has nothing to enumerate for it — yet
    ``.git/modules/<name>`` can still hold its index and unpublished commits
    (after ``git submodule deinit``, for instance). That state cannot be
    proven published from here, so it counts as unpublished.
    """
    stored = _stored_module_repositories(worktree_dir)
    if stored is None:
        return True
    checked_out = _checked_out_submodules(worktree_dir)
    if checked_out is None:
        return True
    if not stored and not checked_out:
        return False
    # Each stored module repository must BE the git dir of a checked-out
    # submodule. Identity, not a count: an old-form submodule with an embedded
    # .git contributes a checked-out line but no stored repository, which a
    # count would let cancel a deinitialized modern sibling.
    live_git_dirs = {_submodule_git_dir(worktree_dir / path) for path in checked_out}
    if None in live_git_dirs:
        return True
    if any(repo.resolve() not in live_git_dirs for repo in stored):
        return True
    # A checked-out submodule whose HEAD no remote-tracking ref contains holds
    # a commit nothing has published. kew publishes the superproject only, so
    # cleanup would delete the sole copy behind a gitlink nobody can fetch.
    return any(_submodule_head_unpublished(worktree_dir / path) for path in checked_out)


def _checked_out_submodules(worktree_dir: Path) -> list[str] | None:
    """Paths of every initialized, checked-out submodule, recursively."""
    status = run(
        _checkout_read_command(worktree_dir, "submodule", "status", "--recursive"),
        check=False,
        env=_checkout_read_env(),
    )
    if status.returncode != 0:
        return None
    return [
        line[1:].split(" ", 2)[1]
        for line in status.stdout.splitlines()
        if line and not line.startswith("-") and len(line.split(" ", 2)) >= 2
    ]


def _submodule_git_dir(path: Path) -> Path | None:
    located = run(
        _checkout_read_command(path, "rev-parse", "--absolute-git-dir"),
        check=False,
        env=_checkout_read_env(),
    )
    if located.returncode != 0 or not located.stdout.strip():
        return None
    return Path(located.stdout.strip()).resolve()


def _submodule_head_unpublished(path: Path) -> bool:
    """Whether no remote-tracking ref of *path* contains its HEAD (local proof)."""
    read_env = _checkout_read_env()
    head = run(
        _checkout_read_command(path, "rev-parse", "HEAD"), check=False, env=read_env
    )
    if head.returncode != 0:
        return True
    containing = run(
        _checkout_read_command(
            path,
            "for-each-ref",
            "--contains",
            head.stdout.strip(),
            "--format=%(refname)",
            "refs/remotes/",
        ),
        check=False,
        env=read_env,
    )
    if containing.returncode != 0:
        return True
    return not containing.stdout.strip()


def _stored_module_repositories(worktree_dir: Path) -> list[Path] | None:
    """Every submodule repository git keeps under this checkout's git dir.

    ``--git-path modules`` is worktree-specific, so a linked worktree's own
    module store is found rather than the common one. Nested submodules store
    theirs beneath the parent module (``modules/<outer>/modules/<inner>``),
    so the walk recurses through each module's own ``modules`` directory.
    ``None`` when git cannot say where the store is: the safe answer.
    """
    located = run(
        _checkout_read_command(worktree_dir, "rev-parse", "--git-path", "modules"),
        check=False,
        env=_checkout_read_env(),
    )
    if located.returncode != 0:
        return None
    if not located.stdout.strip():
        return []
    root = Path(located.stdout.strip())
    if not root.is_absolute():
        root = worktree_dir / root
    if not root.is_dir():
        return []
    found: list[Path] = []
    _walk_module_store(root, found)
    return found


def _walk_module_store(store: Path, found: list[Path]) -> None:
    for child in sorted(store.iterdir()):
        if not child.is_dir():
            continue
        if (child / "HEAD").exists() and (child / "config").exists():
            found.append(child)
            nested = child / "modules"
            if nested.is_dir():
                _walk_module_store(nested, found)
        else:
            # Module names may contain slashes (git stores `a/b` as nested
            # directories), so a directory that is not itself a repository is
            # a path component on the way to one.
            _walk_module_store(child, found)


def _any_flagged_index_entry(listing: str) -> bool:
    for entry in listing.split("\0"):
        if not entry:
            continue
        tag = entry[0]
        if tag == "S" or tag.islower():
            return True
    return False


def _publication_push_command(
    branch: str,
    *,
    force_with_lease: bool,
    expected_remote_sha: str | None,
    repo: str | None,
    head_sha: str,
) -> tuple[list[str], dict[str, str]]:
    """Build a non-interactive detached push of exactly the scanned commit."""
    remote, publication_env = _publication_remote(repo)
    # Explicit, so checkout config (push.followTags) cannot publish tags the
    # pre-push scan never read.
    command = ["git", "push", "--no-follow-tags"]
    if expected_remote_sha is not None:
        command.append(f"--force-with-lease={branch}:{expected_remote_sha}")
    elif force_with_lease:
        command.append("--force-with-lease")
    command.extend([remote, f"{head_sha}:refs/heads/{branch}"])
    push_env = checkout_object_env(
        publication_env if publication_env is not None else os.environ
    )
    return command, push_env


def _publication_remote(repo: str | None) -> tuple[str, dict[str, str] | None]:
    """Return the remote and environment used for host publication."""
    publication_env = publication_git_env()
    from kew.repo_host import repo_host

    remote = (
        f"https://{repo_host()}/{repo}.git"
        if publication_env is not None and repo
        else "origin"
    )
    return remote, publication_env


def publication_remote_sha(
    worktree_dir: Path, branch: str, *, repo: str | None = None
) -> str:
    """Resolve a branch tip without asking git to inspect *worktree_dir*."""
    del worktree_dir
    remote = publication_remote(repo)
    with TemporaryDirectory(prefix="kew-publication-query-") as temp:
        return remote_branch_sha(remote, branch, scratch=Path(temp))


def republish_agent_head(
    worktree_dir: Path,
    branch: str,
    *,
    expected_sha: str,
    observed_sha: str,
    commit_message: str,
    repo: str | None = None,
    store: RunStore | None = None,
    run_id: str | None = None,
    producer_run_id: str | None = None,
    item_id: str | None = None,
) -> CanonicalPublication:
    """Replace an agent-pushed head with a host-built commit of its tree.

    The node's acceptance contracts are read for the run's own item, as every
    canonical publication reads them (#2729); *item_id* is the fallback when
    the ledger holds no row for the run.
    """
    remote = publication_remote(repo)
    item = _intent_item_id(store, run_id=run_id, item_id=item_id, branch=branch)
    with TemporaryDirectory(prefix="kew-publication-republish-") as temp:
        return _republish_agent_head_canonical(
            worktree_dir,
            remote=remote,
            branch=branch,
            expected_sha=expected_sha,
            observed_sha=observed_sha,
            message=commit_message,
            identity=host_commit_identity(),
            output_guard=_output_guard(
                store, run_id=run_id, producer_run_id=producer_run_id
            ),
            scratch=Path(temp),
            acceptance=acceptance_contracts(store, item),
        )


def _output_guard(
    store: RunStore | None,
    *,
    run_id: str | None,
    producer_run_id: str | None,
) -> Callable[[OutputScanTarget], None]:
    """The credential scan every host push of agent output runs first."""
    from kew.publication_credentials import publication_output_guard

    return publication_output_guard(
        store, run_id=run_id, producer_run_id=producer_run_id
    )


def _guard_checkout_output(
    worktree_dir: Path,
    branch: str,
    *,
    expected_remote_sha: str | None,
    store: RunStore | None,
    run_id: str | None,
    producer_run_id: str | None,
    repo: str | None,
    merged_base_sha: str | None = None,
) -> str:
    """Scan every object the escape hatch will push; return the scanned head.

    The excluded tip is the remote head, asked of the remote, never the
    checkout's own agent-writable ``origin/<branch>`` ref.
    """
    base = expected_remote_sha or publication_remote_sha(
        worktree_dir, branch, repo=repo
    )
    head = _local_commit(worktree_dir, "HEAD")
    if not base or not head:
        raise PublicationError(
            "push", f"cannot resolve the commits to scan for branch {branch!r}"
        )
    _output_guard(store, run_id=run_id, producer_run_id=producer_run_id)(
        OutputScanTarget(
            repository=worktree_dir,
            base_sha=base,
            head_sha=head,
            env=checkout_object_env(),
        )
    )
    filter_checkout_range(
        worktree_dir,
        base_sha=base,
        head_sha=head,
        env=checkout_object_env(),
        merged_base_sha=merged_base_sha,
    )
    return head


def _checkout_merged_base_head(
    worktree_dir: Path, merged_base_branch: str | None, *, repo: str | None
) -> str | None:
    """The base branch's remote head for the checkout filter, or None.

    Asked of the publication remote, as canonical's :func:`_merged_base_head`
    is. Best effort: no answer only takes the #2362 exemption away.
    """
    if not merged_base_branch:
        return None
    try:
        return (
            publication_remote_sha(worktree_dir, merged_base_branch, repo=repo) or None
        )
    except Exception as exc:  # noqa: BLE001 — losing the exemption is the cost
        _warn(f"base branch {merged_base_branch!r} could not be resolved: {exc}")
        return None


def _local_commit(worktree_dir: Path, ref: str) -> str:
    result = run(
        _checkout_read_command(
            worktree_dir, "rev-parse", "--verify", f"{ref}^{{commit}}"
        ),
        check=False,
        env=checkout_object_env(),
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_branch_sha(worktree_dir: Path, branch: str) -> str:
    """Fetch and resolve the current remote branch tip."""
    run(["git", "fetch", "origin", branch, "--quiet"], cwd=worktree_dir, check=False)
    result = run(
        ["git", "rev-parse", "--verify", f"origin/{branch}^{{commit}}"],
        cwd=worktree_dir,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _assert_publication_target(
    worktree_dir: Path, branch: str, expected_sha: str
) -> bool:
    """Refuse to publish unless *branch* still points at *expected_sha*.

    Binds a publication to the head the work was actually cut from (#1290). A
    review may run detached at a recorded PR head; if that head moved while the
    reviewer worked, pushing would attribute the remediation to a PR state
    nobody reviewed — and, with ``--force-with-lease``, could discard the new
    commits outright. Identity mismatch is a hard refusal, not a merge.
    Returns ``True`` when the review commit is already present remotely.
    """
    actual = _remote_branch_sha(worktree_dir, branch)
    if not actual:
        raise RuntimeError(
            f"cannot bind publication for branch {branch!r}: origin/{branch} "
            f"is unresolvable"
        )
    if actual == expected_sha:
        return False

    # A runner can publish its own review commit before control returns to the
    # host. Treat that as complete when the local review tip is a clean
    # descendant of the bound head and the observed remote tip contains it.
    # This also covers a subsequent ordinary fast-forward built on that review
    # commit; an unrelated move remains a hard refusal.
    local = run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=worktree_dir,
        check=False,
    )
    local_sha = local.stdout.strip() if local.returncode == 0 else ""
    if (
        local_sha
        and local_sha != expected_sha
        and not has_uncommitted_changes(worktree_dir)
        and run(
            ["git", "merge-base", "--is-ancestor", expected_sha, local_sha],
            cwd=worktree_dir,
            check=False,
        ).returncode
        == 0
        and run(
            ["git", "merge-base", "--is-ancestor", local_sha, actual],
            cwd=worktree_dir,
            check=False,
        ).returncode
        == 0
    ):
        return True

    raise PublicationHeadMovedError(expected_sha, actual, branch)


def _commit_publication_worktree(worktree_dir: Path, commit_message: str) -> None:
    """Commit the runner's edits, preserving typed publication failures.

    Built by plumbing under an inert git environment, so no hook, fsmonitor,
    filter or signing program the checkout names runs on the host (#2634).
    """
    try:
        commit_checkout_worktree(worktree_dir, commit_message)
    except PublicationError:
        raise
    except Exception as exc:
        raise PublicationError("commit", exc) from exc


def _publication_ahead_count(worktree_dir: Path, branch: str) -> int:
    """Prove how many commits the local review is ahead of the remote."""
    ahead = run(
        ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
        cwd=worktree_dir,
        check=False,
    )
    if ahead.returncode != 0 or not ahead.stdout.strip().isdigit():
        raise PublicationError(
            "push", f"cannot prove publication state for branch {branch!r}"
        )
    return int(ahead.stdout.strip())


def _push_publication(
    worktree_dir: Path,
    branch: str,
    *,
    force_with_lease: bool,
    expected_remote_sha: str | None,
    repo: str | None,
    head_sha: str,
) -> None:
    """Push a proven-ahead review worktree and classify transport failures."""
    command, push_env = _publication_push_command(
        branch,
        force_with_lease=force_with_lease,
        expected_remote_sha=expected_remote_sha,
        repo=repo,
        head_sha=head_sha,
    )
    pushed = run(command, cwd=worktree_dir, check=False, env=push_env)
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout or "").strip()[:200]
        if expected_remote_sha is not None:
            actual = _remote_branch_sha(worktree_dir, branch)
            if actual != expected_remote_sha or "stale info" in detail.lower():
                raise PublicationHeadMovedError(
                    expected_remote_sha, actual or "unresolved", branch
                )
        raise PublicationError(
            "push", f"host publication failed for branch {branch!r}: {detail}"
        )


def publish_existing_pr_branch(
    worktree_dir: Path,
    branch: str,
    *,
    commit_message: str,
    force_with_lease: bool = False,
    expected_remote_sha: str | None = None,
    repo: str | None = None,
    publish: Literal["canonical", "checkout"] = "canonical",
    dangerously_skip_containment: bool = False,
    store: RunStore | None = None,
    run_id: str | None = None,
    item_id: str | None = None,
    producer_run_id: str | None = None,
    merged_base_branch: str | None = None,
    integrated_base_sha: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> bool | CanonicalPublication | CanonicalNoChange:
    """Publish runner edits onto an existing PR branch from the host.

    Canonical mode returns explicit publication or no-change evidence.
    Checkout mode returns a boolean. Indeterminate publication raises.

    *expected_remote_sha* binds the push to the PR head the caller intends to
    update. The pre-commit check avoids unnecessary local commits after known
    drift. The final push also carries an exact force-with-lease, which closes
    the check-to-push race. When detached publication auth and *repo* are
    available, the push uses authenticated HTTPS. ``before_push`` runs only
    when a new head will be pushed (#2033).

    *merged_base_branch* names the branch this checkout may have merged — the
    PR's own base. Canonical mode resolves its head from the remote and reads
    it for the protected-path review flags only (#2362): a workflow file whose
    bytes are already the base branch's own was not authored in this checkout.
    Omitted, every protected path in the publication diff stops for a person,
    base merge included.

    *integrated_base_sha* is the base commit kew's OWN base integration merged
    into this checkout before the agent ran. Canonical mode publishes it as the
    commit's SECOND parent, so the merge the host really made is recorded
    instead of flattened (#2525, :func:`_integration_parent`). Supplying it also
    makes the #2362 exemption exact — that commit's content, not whatever the
    base branch has drifted to since — and arms the copy-sync hold (#2457):
    content that is the base branch's current head's own, which neither the
    branch nor that integrated commit holds, was copied in instead of merged.
    Omitted, the publication has the branch head as its only parent, nothing is
    judged for a copy, and the exemption falls back to the base branch's head as
    before.
    """
    if store is not None and (run_id or producer_run_id):
        from kew.publication_credentials import ensure_publication_credentials

        ensure_publication_credentials(
            store,
            run_id=run_id or producer_run_id or "",
            producer_run_id=producer_run_id,
        )
    mode = select_publication_mode(
        publish,
        dangerously_skip_containment=dangerously_skip_containment,
        store=store,
        run_id=run_id,
        item_id=item_id,
        branch=branch,
        repo=repo,
    )
    if mode == "canonical":
        return _publish_existing_pr_branch_canonical(
            worktree_dir,
            branch,
            commit_message=commit_message,
            expected_remote_sha=expected_remote_sha,
            repo=repo,
            store=store,
            run_id=run_id,
            item_id=_intent_item_id(
                store, run_id=run_id, item_id=item_id, branch=branch
            ),
            producer_run_id=producer_run_id,
            merged_base_branch=merged_base_branch,
            integrated_base_sha=integrated_base_sha,
            before_push=before_push,
        )
    return _publish_existing_pr_branch_checkout(
        worktree_dir,
        branch,
        commit_message=commit_message,
        force_with_lease=force_with_lease,
        expected_remote_sha=expected_remote_sha,
        repo=repo,
        store=store,
        run_id=run_id,
        item_id=item_id,
        producer_run_id=producer_run_id,
        merged_base_branch=merged_base_branch,
        integrated_base_sha=integrated_base_sha,
        before_push=before_push,
    )


def _publish_existing_pr_branch_checkout(
    worktree_dir: Path,
    branch: str,
    *,
    commit_message: str,
    force_with_lease: bool,
    expected_remote_sha: str | None,
    repo: str | None,
    store: RunStore | None,
    run_id: str | None,
    item_id: str | None,
    producer_run_id: str | None,
    merged_base_branch: str | None,
    integrated_base_sha: str | None,
    before_push: Callable[[], object] | None,
) -> bool:
    """The checkout escape hatch: commit, scan, then push only a new head.

    The merged base is judged as canonically (#2362): kew's own integration,
    else the base branch's remote head, never the checkout's refs.
    """
    if expected_remote_sha is not None and _assert_publication_target(
        worktree_dir, branch, expected_remote_sha
    ):
        return False

    _commit_publication_worktree(worktree_dir, commit_message)
    if _publication_ahead_count(worktree_dir, branch) == 0:
        return False
    scanned_head = _guard_checkout_output(
        worktree_dir,
        branch,
        expected_remote_sha=expected_remote_sha,
        store=store,
        run_id=run_id,
        producer_run_id=producer_run_id,
        repo=repo,
        merged_base_sha=integrated_base_sha
        or _checkout_merged_base_head(worktree_dir, merged_base_branch, repo=repo),
    )
    if before_push is not None:
        before_push()
    _record_checkout_intent(
        worktree_dir,
        store=store,
        run_id=run_id,
        item_id=item_id,
        branch=branch,
        expected_remote_sha=expected_remote_sha,
    )
    _push_publication(
        worktree_dir,
        branch,
        force_with_lease=force_with_lease,
        expected_remote_sha=expected_remote_sha,
        repo=repo,
        head_sha=scanned_head,
    )
    return True


def _record_checkout_intent(
    worktree_dir: Path,
    *,
    store: RunStore | None,
    run_id: str | None,
    item_id: str | None,
    branch: str,
    expected_remote_sha: str | None,
) -> None:
    """Record the checkout escape hatch's commit before its push."""
    on_intent = _intent_recorder(
        store,
        run_id=run_id,
        item_id=item_id,
        branch=branch,
        parent_sha=expected_remote_sha or "",
    )
    if on_intent is not None:
        head = run(["git", "rev-parse", "HEAD"], cwd=worktree_dir).stdout.strip()
        tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=worktree_dir).stdout.strip()
        on_intent(head, tree)


def _intent_item_id(
    store: RunStore | None,
    *,
    run_id: str | None,
    item_id: str | None,
    branch: str | None = None,
) -> str | None:
    """Return the run's tracker item, or None for branch scope.

    Standalone review PR numbers are accounting keys, not tracker items.
    A retained run's identity takes precedence over caller fallbacks.
    A standalone review's row names no item of its own, so the item comes from
    the branch it publishes (#2446) — the same join the next round's producer
    lookup reads, so a hand-started ``kew review <PR>`` of an owned branch
    records its intent under the owning item instead of under a branch scope
    that lookup no longer consults.
    """
    if store is not None and run_id:
        from kew.publication_credentials import publication_run_item

        record = store.get(run_id)
        if record is not None:
            return publication_run_item(record) or _branch_owner_intent_item(
                store, record, branch
            )
    return item_id


def _branch_owner_intent_item(
    store: RunStore, record: Any, branch: str | None
) -> str | None:
    """The item owning *branch*, for a row whose own ``item_id`` cannot say.

    Only a row that abstains from its branch's ownership is resolved this way,
    and only against the branch it is publishing. An unreadable or ambiguously
    owned branch keeps branch scope — what the publisher recorded before #2446 —
    so a read that cannot name an owner never costs the push itself; the next
    round then binds the head through the legacy producer path as it did then.
    """
    from kew.publication_credentials import (
        _single_branch_item,
        abstains_from_branch_ownership,
        branch_owner_beside_its_prs,
    )

    if not branch or not abstains_from_branch_ownership(record):
        return None
    try:
        # A branch whose other runs only served its own PR is still owned by
        # its node: record the claim under that item (#3193).
        return _single_branch_item(store, branch=branch) or branch_owner_beside_its_prs(
            store, branch=branch
        )
    except Exception as exc:  # noqa: BLE001 — branch scope is the safe fallback
        _warn(
            f"Publication intent for {branch!r}: cannot resolve the branch's "
            f"owning item ({exc}); recording branch scope instead."
        )
        return None


def _intent_recorder(
    store: RunStore | None,
    *,
    run_id: str | None,
    item_id: str | None,
    branch: str,
    parent_sha: str,
) -> Callable[[str, str], None] | None:
    """The ``on_intent`` hook that binds a remediation head to its run.

    Written before the push, exactly like a fresh dispatch, so the producer
    lookup for the next round binds the head through ``publication_intent``.
    Item scope uses the run's tracker item. Branch scope uses the PR head branch.
    Branch scope applies when neither the run nor its branch names a tracker
    item (:func:`_intent_item_id`).
    That lookup then requires the run row to name the branch, so an intent is
    recorded only for a run that durably owns it (``_run_owns_branch``): a
    run without a row, or whose row names no branch, records none and the
    legacy heuristic applies as before; a run that names another branch, or
    a kind that never publishes, refuses the publication outright.
    """
    if store is None:
        return None
    if not _run_owns_branch(store, run_id=run_id, branch=branch):
        return None
    item_id = _intent_item_id(store, run_id=run_id, item_id=item_id, branch=branch)

    def on_intent(commit_sha: str, tree_sha: str) -> None:
        from kew.dispatch_core import _record_publication_intent

        _record_publication_intent(
            store,
            run_id=run_id,
            item_id=item_id,
            branch=branch,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            parent_sha=parent_sha,
        )

    return on_intent


def _run_owns_branch(store: RunStore, *, run_id: str | None, branch: str) -> bool:
    """Whether *run_id*'s row durably names *branch* as the branch it worked on."""
    from kew.publication_credentials import _NON_PUBLICATION_RUN_KINDS

    record = store.get(run_id) if run_id else None
    if record is None:
        _warn(
            f"Publication of {branch!r} records no intent: run {run_id!r} has "
            "no row, so the next round cannot bind the head to it."
        )
        return False
    kind = getattr(record, "run_kind", None)
    if kind in _NON_PUBLICATION_RUN_KINDS:
        raise PublicationError(
            "push",
            f"run {run_id} is a {kind} run and publishes nothing; refusing to "
            f"publish {branch!r} under it",
        )
    recorded = getattr(record, "branch", None)
    if recorded == branch:
        return True
    if not recorded:
        _warn(
            f"Publication of {branch!r} records no intent: run {run_id} names "
            "no branch, so the next round cannot bind the head to it."
        )
        return False
    raise PublicationError(
        "push",
        f"run {run_id} belongs to branch {recorded!r}, not {branch!r}; "
        "refusing to publish",
    )


def _merged_base_head(
    remote: str, merged_base_branch: str | None, *, scratch: Path
) -> str | None:
    """The remote head of the branch this checkout may have merged, or None.

    Resolved from the REMOTE, never from the checkout, and read only by the
    copy-sync rule as the base head to judge against. It never earns the
    protected-path exemption: no parent of the published commit is this head
    (#2632). Best effort — an unresolvable branch leaves the rule unarmed.
    """
    if not merged_base_branch:
        return None
    try:
        return remote_branch_sha(remote, merged_base_branch, scratch=scratch) or None
    except Exception as exc:  # noqa: BLE001 — losing the copy-sync rule is the cost
        _warn(f"base branch {merged_base_branch!r} could not be resolved: {exc}")
        return None


def _copy_sync_guard(
    *,
    integrated_base_sha: str | None,
    upstream_head: str | None,
    merged_base_branch: str | None,
) -> CopySync | None:
    """The base head this publication must not introduce by copy, or None.

    All three facts are required (#2457). Without the commit kew integrated
    there is no way to tell the base content the HOST brought from the base
    content an agent copied, and holding on that ambiguity would stop every
    remediation of a branch whose base had moved.
    """
    if not integrated_base_sha or not upstream_head or not merged_base_branch:
        return None
    return CopySync(
        sha=upstream_head,
        label=f"origin/{merged_base_branch}",
        integrated=integrated_base_sha,
    )


def _merge_is_in_checkout_history(worktree_dir: Path, integrated_base_sha: str) -> bool:
    """Whether the checkout's git state still carries the host's merge.

    Two states carry it, and the agent is told to leave its resolution staged
    and uncommitted, so both have to count: the integration is in the
    checkout's history (``HEAD``), or it is the merge still in progress
    (``MERGE_HEAD``), whose own tree is the resolution being published.
    """
    read_env = checkout_object_env()
    for revision in ("HEAD", "MERGE_HEAD"):
        held = run(
            _checkout_read_command(
                worktree_dir,
                "merge-base",
                "--is-ancestor",
                integrated_base_sha,
                revision,
            ),
            check=False,
            env=read_env,
        )
        if held.returncode == 0:
            return True
    return False


def _checkout_merge_base(worktree_dir: Path, left: str, right: str) -> str | None:
    """Where the two commits forked, or None when the checkout cannot say."""
    forked = run(
        _checkout_read_command(worktree_dir, "merge-base", left, right),
        check=False,
        env=checkout_object_env(),
    )
    sha = (forked.stdout or "").strip()
    return sha if forked.returncode == 0 and sha else None


def _checkout_changed_paths(
    worktree_dir: Path, *revisions: str
) -> frozenset[str] | None:
    """Paths a diff touches, or None when the checkout will not answer.

    One *revisions* entry diffs that commit against the WORKING TREE — the
    content publication actually extracts — rather than against a commit the
    agent may have left the tree disagreeing with.
    """
    listed = run(
        _checkout_read_command(worktree_dir, "diff", "--name-only", "-z", *revisions),
        check=False,
        env=checkout_object_env(),
    )
    if listed.returncode != 0:
        return None
    return frozenset(path for path in (listed.stdout or "").split("\0") if path)


def _merge_content_in_published_tree(
    worktree_dir: Path, integrated_base_sha: str, head_sha: str
) -> bool:
    """Whether the tree being published still holds what the merge brought in.

    History is not content. ``git revert -m 1 HEAD`` and ``git checkout
    <pr-head> -- .`` both leave the merge in the checkout's history while
    undoing every byte of it, so an ancestry answer alone still declares the
    parent and landing the PR then reverts the base's own commit (#2525
    review). This is the same question asked of the tree: of the paths the
    integrated commit advanced past the fork point, is any one of them back at
    its pre-integration content?

    Asked of content, not of paths (#2632). The pre-integration content is the
    PR head's, so every path the merge changed has to differ from the PR head
    in the published tree. Asking only whether the path changed since the fork
    point let a branch that had edited the same path itself revert the base's
    change there and still read as holding it. A path where the integrated
    base and the PR head already agree brought nothing in and asks nothing,
    and neither does one both sides edited where git's own merge keeps the PR
    head's bytes because the head already carried the base's change.
    Only those paths are merged, in memory, and no object is written into an
    agent-writable checkout: an edit on top of the base's content still counts
    as held, even if it dropped some of the base's lines. The guarantee is one
    direction — a path put back to its pre-integration content cannot be
    declared.
    """
    fork_point = _checkout_merge_base(worktree_dir, integrated_base_sha, head_sha)
    if fork_point is None:
        return False
    advanced = _checkout_changed_paths(worktree_dir, fork_point, integrated_base_sha)
    differs = _checkout_changed_paths(worktree_dir, head_sha, integrated_base_sha)
    edited = _checkout_changed_paths(worktree_dir, fork_point, head_sha)
    published = _checkout_changed_paths(worktree_dir, head_sha)
    if advanced is None or differs is None or edited is None or published is None:
        return False
    revisions = (head_sha, fork_point, integrated_base_sha)
    brought_in = {
        path
        for path in advanced & differs
        if path not in edited or not _merge_keeps_head(worktree_dir, revisions, path)
    }
    return not brought_in - published


def _merge_keeps_head(
    worktree_dir: Path, revisions: tuple[str, str, str], path: str
) -> bool:
    """Whether a clean three-way merge of *path* is the PR head's own bytes.

    A path both sides edited may differ from the base and still have nothing
    to bring in: the PR head already carried the base's change, and git keeps
    the head's content (#2632 review). Merged in memory — ``-p`` prints the
    result instead of writing it into the agent-writable object store — and a
    conflict, a missing side or an unreadable answer all say "brought in", so
    the only path excused is one git itself would have left untouched.
    """
    objects = tuple(f"{revision}:{path}" for revision in revisions)
    try:
        merged = run(
            _checkout_read_command(
                worktree_dir, "merge-file", "-p", "--object-id", *objects
            ),
            check=False,
            env=checkout_object_env(),
        )
        if merged.returncode != 0:
            return False
        current = run(
            _checkout_read_command(worktree_dir, "cat-file", "blob", objects[0]),
            check=False,
            env=checkout_object_env(),
        )
    except UnicodeDecodeError as exc:
        _warn(f"{path!r} could not be merged as text, read as brought in: {exc}")
        return False
    return current.returncode == 0 and current.stdout == merged.stdout


def _integration_not_held(
    worktree_dir: Path, integrated_base_sha: str, head_sha: str
) -> str | None:
    """Why the tree about to be published does not carry the host's merge.

    ``None`` when it does carry it. kew's record says it merged that commit
    into the checkout; it does not say the merge survived the agent. A
    conflicted integration is handed to the agent to resolve, and an agent that
    answers ``git merge --abort`` leaves a checkout holding none of the base's
    content. Declaring the parent from the record alone then publishes a commit
    whose own tree contradicts it: the next round's integration answers
    "Already up to date", and merging the PR REVERTS the base commit the
    publication claimed to contain (#2525 review).

    Both halves of "survived" are asked, because either one alone is a hole:
    the merge has to still be in the checkout's git state, AND the content it
    brought in has to still be in the tree being published.

    The checkout is read only to VETO the host's record, never to source it: a
    yes adds nothing kew did not already know, so no answer here can promote an
    agent's claim into a parent. Replace refs and grafts are off — an
    agent-writable checkout would otherwise get to redefine ancestry — and a
    checkout that will not answer drops the record instead of claiming it.
    """
    if not _merge_is_in_checkout_history(worktree_dir, integrated_base_sha):
        return "the base integration was abandoned"
    if not _merge_content_in_published_tree(
        worktree_dir, integrated_base_sha, head_sha
    ):
        return "the content the base integration brought in was undone"
    return None


def _integration_parent(
    remote: str,
    *,
    integrated_base_sha: str | None,
    head_sha: str,
    worktree_dir: Path,
    scratch: Path,
) -> tuple[str, ...]:
    """The second parent that records kew's own base integration, or ``()``.

    kew materializes a remediation checkout by merging ``origin/<base>`` into
    the PR branch (#2362) and the agent edits on top, so the tree published
    afterwards is a MERGE of two commits. Recording only the branch head
    flattened that merge, and the base's own newer files then read as this
    node's work: PR #2461's fix commit was 62 files and 5303 insertions, four of
    them byte copies of ``main``, and when the base moved again all four
    conflicted (#2525). Declaring the integrated commit as a second parent puts
    the PR's merge base back where the host actually integrated, so the diff is
    the branch's own work and a later base move merges cleanly.

    The sha is the host's own record of what IT merged — never a value read
    back out of the agent's checkout, where a second parent would be the
    agent's claim rather than kew's fact.

    Nothing is declared unless there is a real integration to declare:

    * No recorded commit (a checkout from before #2362, or an unreadable ref):
      there is no merge the host can attest to.
    * A commit the branch already contains: the integration answered "Already
      up to date" and brought nothing in, so a parent would record a merge that
      never happened — and, worse, tell a later merge that content it has not
      seen is already accounted for.
    * A merge the published tree no longer holds, in git state or in content:
      see :func:`_integration_not_held`. The record is kew's fact about what it
      merged, not a promise the agent kept it.

    An unprovable ancestry answer is none of these, and it HOLDS the
    publication rather than drop the parent (#2632): dropping it flattened the
    host's merge again — the #2525 defect — on any remote error, and nothing
    about the integration had been shown false.
    """
    if not integrated_base_sha:
        return ()
    not_held = _integration_not_held(
        worktree_dir, integrated_base_sha, head_sha=head_sha
    )
    if not_held:
        _warn(
            f"integrated base {integrated_base_sha[:12]} is not held by the "
            f"checkout being published, publishing without it: {not_held}"
        )
        return ()
    try:
        already_integrated = remote_commit_is_ancestor(
            remote, integrated_base_sha, head_sha, scratch=scratch
        )
    except Exception as exc:  # noqa: BLE001 — every unproven answer holds
        raise PublicationHeld(
            (
                f"integrated base {integrated_base_sha[:12]} could not be proven "
                f"against {head_sha[:12]}, and publishing without it would "
                f"flatten the host's base integration: {exc}",
            )
        ) from exc
    return () if already_integrated else (integrated_base_sha,)


def _publish_existing_pr_branch_canonical(
    worktree_dir: Path,
    branch: str,
    *,
    commit_message: str,
    expected_remote_sha: str | None,
    repo: str | None,
    store: RunStore | None = None,
    run_id: str | None = None,
    item_id: str | None = None,
    producer_run_id: str | None = None,
    merged_base_branch: str | None = None,
    integrated_base_sha: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> CanonicalPublication | CanonicalNoChange:
    remote = publication_remote(repo)
    with TemporaryDirectory(prefix="kew-canonical-publication-") as temp:
        scratch = Path(temp)
        actual = remote_branch_sha(remote, branch, scratch=scratch / "query")
        if not actual:
            raise PublicationError(
                "push", f"cannot resolve publication branch {branch!r}"
            )
        if expected_remote_sha is not None and actual != expected_remote_sha:
            raise PublicationHeadMovedError(expected_remote_sha, actual, branch)
        on_intent = _intent_recorder(
            store, run_id=run_id, item_id=item_id, branch=branch, parent_sha=actual
        )
        upstream_head = _merged_base_head(
            remote, merged_base_branch, scratch=scratch / "merged-base-query"
        )
        try:
            integration = _integration_parent(
                remote,
                integrated_base_sha=integrated_base_sha,
                head_sha=actual,
                worktree_dir=worktree_dir,
                scratch=scratch / "integration-ancestry",
            )
            publication = publish_checkout(
                worktree_dir,
                remote=remote,
                branch=branch,
                parents=(actual, *integration),
                base_sha=actual,
                # Only the merge this commit declares earns the protected-path
                # exemption (#2632): a rejected integration, or the base's
                # current head, would excuse bytes no parent accounts for.
                merged_base_sha=integration[0] if integration else None,
                copy_sync=_copy_sync_guard(
                    integrated_base_sha=integrated_base_sha,
                    upstream_head=upstream_head,
                    merged_base_branch=merged_base_branch,
                ),
                acceptance=acceptance_contracts(store, item_id),
                message=commit_message,
                identity=host_commit_identity(),
                force_with_lease=f"{branch}:{actual}",
                on_intent=on_intent,
                before_push=before_push,
                output_guard=_output_guard(
                    store, run_id=run_id, producer_run_id=producer_run_id
                ),
                # A remote that fails this push on its own side gets the same
                # commit again before the run is failed: the alternative is the
                # epic loop redoing a finished remediation from scratch (#2455).
                push_attempts=PUSH_ATTEMPTS,
                push_delays=PUSH_DELAYS,
                scratch=scratch / "publish",
            )
        except PublicationHeld as exc:
            raise PublicationError("push", exc) from exc
        except PublicationError as exc:
            latest = remote_branch_sha(
                remote, branch, scratch=scratch / "failed-push-query"
            )
            if latest and latest != actual:
                raise PublicationHeadMovedError(actual, latest, branch) from exc
            raise
    return publication


def cleanup_worktree(worktree_dir: Path, branch: str) -> None:
    """Remove a checkout and the branch **kew** created for it.

    *branch* is supplied by the caller, and is required rather than optional so
    the type checker enumerates the call sites instead of leaving a silent
    fallback. It used to be read back out of *worktree_dir* with ``git branch
    --show-current`` — a directory the agent has written — which made a
    destructive host ``git branch -D`` take agent-chosen input:

    * **#1445**: an agent runs ``git checkout <any operator branch not
      currently checked out>``; cleanup then force-deletes it, unmerged work
      included, while the branch kew actually created survives orphaned.
    * **#1487**: the review checkout is a *clone*, so it sits on the PR branch
      by name and cleanup deleted the operator's own local copy of it —
      no agent required, on every successful review.

    kew authored the branch and knows its name, so passing it makes the
    argument host-authored by construction and leaves nothing to validate.

    Removal goes through :func:`discard_checkout`, which handles a linked
    worktree and a clone alike; the previous ``git worktree remove`` silently
    did nothing to a clone and left it on disk. The branch delete is addressed
    with ``git -C`` at the repository root rather than inheriting the process
    working directory.
    """
    if not worktree_dir.exists():
        return

    discard_checkout(worktree_dir)
    if not branch:
        return
    root = _repo_root_or_none()
    prefix = ["git", "-C", str(root)] if root is not None else ["git"]
    run([*prefix, "branch", "-D", branch], check=False, capture=True)
