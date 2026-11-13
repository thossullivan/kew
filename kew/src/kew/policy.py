"""Policy binding: hash the governance policy in force at dispatch (#250).

A SHA-256 over a redacted, allowlisted snapshot of the governance config plus
the deployed security-hook digest. The snapshot is *construct-only* — an
explicit allowlist, never copy-then-strip — so no secret (e.g.
``alerting.webhook_url``) can reach the retained preimage.

Pure except ``repo_root()`` (stats ancestors' ``.git`` entries) and ``hook_digest()``
(reads the hook file).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kew.config import DispatchConfig, RouteEntry, RoutingConfig

POLICY_SNAPSHOT_VERSION = 1
HOOK_REL_PATH = ".claude/hooks/security-guard.sh"
HOOK_ABSENT = "absent"
HOOK_UNREADABLE = "unreadable"

_logger = logging.getLogger(__name__)

#: The most of a gitdir file or ``commondir`` marker read: a path, never more.
_GIT_POINTER_MAX_BYTES = 4096


def _read_git_pointer(path: Path) -> str:
    """The text of a gitdir file or ``commondir`` marker, read as git reads it.

    Only a regular file is read, and at most ``_GIT_POINTER_MAX_BYTES``: the
    open never blocks (a FIFO would wait on a writer), never takes a terminal
    as the controlling tty, and is checked on the descriptor, so a swap between
    check and read cannot read from a device either.
    Raises ``OSError`` for anything else, ``UnicodeDecodeError`` for non-text.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        data = os.read(fd, _GIT_POINTER_MAX_BYTES)
    finally:
        os.close(fd)
    return data.decode("utf-8")


def _git_dir_refusal(git_dir: Path) -> str | None:
    """Why git would refuse *git_dir* as a repository directory, else ``None``.

    Mirrors git's own test: a ``HEAD`` file, and ``objects/`` and ``refs/``
    under the common directory (a linked worktree's gitdir names it in
    ``commondir``).
    """
    if not (git_dir / "HEAD").is_file():
        return f"{git_dir} has no HEAD"
    common = git_dir
    marker = git_dir / "commondir"
    if marker.is_file():
        try:
            common = git_dir / _read_git_pointer(marker).strip()
        except (OSError, UnicodeDecodeError) as exc:
            return f"{marker} is unreadable: {exc}"
    if not ((common / "objects").is_dir() and (common / "refs").is_dir()):
        return f"{common} has no objects/ and refs/"
    return None


def _git_entry_refusal(entry: Path) -> str | None:
    """Why git would refuse *entry* (a ``.git`` dir or gitdir file), else ``None``."""
    if entry.is_dir():
        return _git_dir_refusal(entry)
    try:
        prefix, _, value = _read_git_pointer(entry).strip().partition(":")
    except (OSError, UnicodeDecodeError) as exc:
        return f"{entry} is unreadable: {exc}"
    if prefix.strip() != "gitdir" or not value.strip():
        return f"{entry} names no gitdir"
    return _git_dir_refusal(entry.parent / value.strip())


def repo_root(start: Path | None = None) -> Path:
    """Nearest ancestor of *start* (default cwd) with a valid ``.git``, else *start*.

    ``.git`` is a directory in a normal clone and a file in a worktree; both
    anchor here, so the hook path is cwd-independent within a repo or worktree.
    A ``.git`` git itself would refuse is skipped: an empty directory, which
    ``git rev-parse --show-toplevel`` skips too, and a file whose ``gitdir:``
    target is gone, from inside which git reports no toplevel at all — so the
    root is the toplevel git reports above it, never the stale checkout (#3108).
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        entry = candidate / ".git"
        if not (entry.exists() or entry.is_symlink()):
            continue
        refusal = _git_entry_refusal(entry)
        if refusal is None:
            return candidate
        _logger.debug("repo_root skips %s: %s", entry, refusal)
    return current


def hook_digest(hook_path: Path) -> str:
    """SHA-256 hex of the deployed hook bytes; sentinels instead of raising.

    ``HOOK_ABSENT`` when the file is missing, ``HOOK_UNREADABLE`` on any other
    ``OSError`` (e.g. the path is a directory or permission-denied).
    """
    try:
        data = hook_path.read_bytes()
    except FileNotFoundError:
        return HOOK_ABSENT
    except OSError:
        return HOOK_UNREADABLE
    return hashlib.sha256(data).hexdigest()


def _route(entry: RouteEntry | str | None) -> dict[str, str] | None:
    """Canonicalize one routing tier to ``{"runner","model"}`` or ``None``."""
    if entry is None:
        return None
    if isinstance(entry, str):
        # String shorthand: the runner name; model inherited from agent config.
        return {"runner": entry, "model": ""}
    return {"runner": entry.runner, "model": entry.model}


def _routing(routing: RoutingConfig | None) -> dict[str, Any] | None:
    if routing is None:
        return None
    return {
        "default": _route(routing.default),
        "simple": _route(routing.simple),
        "medium": _route(routing.medium),
        "complex": _route(routing.complex),
    }


def policy_snapshot(config: DispatchConfig, hook_digest_value: str) -> dict[str, Any]:
    """Allowlisted, canonical governance snapshot. Construct-only — no strip."""
    return {
        "version": POLICY_SNAPSHOT_VERSION,
        "budget": {
            "per_issue_usd": config.budget.per_issue_usd,
            "daily_usd": config.budget.daily_usd,
            "weekly_usd": config.budget.weekly_usd,
        },
        "approval": {
            "threshold_usd": config.approval.threshold_usd,
            "label": config.approval.label,
            # Order-independent allowlist — sort so a reordered list is the
            # same policy (hashes identically).
            "approvers": sorted(config.approval.approvers),
        },
        "routing": _routing(config.routing),
        "security": {
            "blocked_prefix_patterns": list(config.security.blocked_prefix_patterns),
            "blocked_exact_patterns": list(config.security.blocked_exact_patterns),
        },
        "limits": {
            "agent_max_budget_usd": config.agent.max_budget_usd,
            "agent_timeout_minutes": config.agent.timeout_minutes,
            "retry_max_attempts": config.retry.max_attempts,
            "parallel_max_workers": config.parallel.max_workers,
            # The concurrency ceiling actually IN FORCE (#2398). `[fleet]
            # max_concurrent` overrides `[parallel] max_workers` for every
            # admission check, so attesting only the latter would let an
            # operator raise the real cap from 3 to 10 and leave policy_hash
            # byte-identical — an event attesting a ceiling nothing enforces.
            # Both are recorded: max_workers is still a configured value, and
            # this is the number the gate reads.
            "fleet_max_concurrent": config.fleet_concurrency(),
            "breaker_enabled": config.breaker.enabled,
            "breaker_cost_anomaly_factor": config.breaker.cost_anomaly_factor,
            "breaker_min_samples": config.breaker.min_samples,
        },
        "run_reports": {"sink_path": config.run_reports.sink_path},
        "security_hook": {"path": HOOK_REL_PATH, "sha256": hook_digest_value},
    }


def canonical_json(snapshot: dict[str, Any]) -> str:
    """Deterministic JSON for hashing a dict (sort_keys because it's a dict)."""
    return json.dumps(
        snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )


def policy_hash(snapshot: dict[str, Any]) -> str:
    """SHA-256 hex over ``canonical_json(snapshot)``."""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def compute_policy(
    config: DispatchConfig, *, hook_path: Path | None = None
) -> tuple[str, str]:
    """Return ``(policy_hash_hex, snapshot_json)`` for *config*.

    *hook_path* defaults to ``repo_root() / HOOK_REL_PATH`` — anchored at the
    git/worktree root, NOT cwd — so the same deployed policy hashes identically
    from any subdir.
    """
    path = hook_path if hook_path is not None else repo_root() / HOOK_REL_PATH
    snapshot = policy_snapshot(config, hook_digest(path))
    snap_json = canonical_json(snapshot)
    digest = hashlib.sha256(snap_json.encode("utf-8")).hexdigest()
    return digest, snap_json
