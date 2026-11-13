"""Resolve the project repository host without importing dispatch layers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kew.config import project_root
from kew.subprocess_utils import run

_DEFAULT_PROJECT_HOST = "github.com"
_REPO_HOST_BY_ROOT: dict[str, str] = {}


def _repo_host_from_remote_url(remote_url: str) -> str | None:
    """Extract the project host from a two-segment Git remote URL."""
    value = remote_url.strip()
    if "://" not in value and ":" in value:
        authority, _, path = value.partition(":")
        host = authority.rpartition("@")[2]
    else:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not host:
            return None
        path = parsed.path
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return host


def _origin_remote_url(
    work_dir: Path, *, run_fn: Callable[..., Any] | None = None
) -> str | None:
    """Read the worktree's origin URL, failing softly."""
    runner = run_fn or run
    try:
        result = runner(
            ["git", "remote", "get-url", "origin"],
            cwd=work_dir,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    stdout = getattr(result, "stdout", None)
    return stdout.strip() or None if isinstance(stdout, str) else None


def _repo_host_from_gh(root: Path, runner: Callable[..., Any]) -> str | None:
    """Use ``gh repo view --json url`` as a fail-soft fallback."""
    try:
        result = runner(
            ["gh", "repo", "view", "--json", "url"],
            cwd=root,
            check=False,
        )
        payload: Any = json.loads(getattr(result, "stdout", "") or "{}")
    except Exception:  # noqa: BLE001
        return None
    if getattr(result, "returncode", 1) != 0 or not isinstance(payload, dict):
        return None
    url = payload.get("url")
    return _repo_host_from_remote_url(url) if isinstance(url, str) else None


def repo_host(
    work_dir: Path | None = None, *, run_fn: Callable[..., Any] | None = None
) -> str:
    """Resolve and cache the project host by resolved repository root."""
    root = (work_dir or project_root()).resolve()
    cache_key = str(root)
    cached = _REPO_HOST_BY_ROOT.get(cache_key)
    if cached is not None:
        return cached

    runner = run_fn or run
    remote_url = _origin_remote_url(root, run_fn=runner)
    host = _repo_host_from_remote_url(remote_url) if remote_url is not None else None
    if host is None:
        host = _repo_host_from_gh(root, runner)
    resolved = host or _DEFAULT_PROJECT_HOST
    _REPO_HOST_BY_ROOT[cache_key] = resolved
    return resolved
