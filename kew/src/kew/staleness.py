"""Startup staleness nudge (#394).

Non-blocking, cached, opt-out-able check that warns when the running kew
build is behind the latest ``kew-cli`` release on PyPI or is an editable install whose
source checkout is behind its local ``origin/main`` ref.

Also: preflight nudge (#433) when the running kew is behind the target repo's
pyproject.toml version or the dispatch.db schema version.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

from kew.links import UPGRADE_HINT
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.run_store import RunStore

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86_400  # 24 hours
_PYPI_LATEST_URL = "https://pypi.org/pypi/kew-cli/json"
_OPT_OUT_ENV = "KEW_NO_UPDATE_CHECK"

# A PEP 440 public release, pre-release, post or dev version: digits and dots
# with an optional a/b/rc, .post and .dev suffix. The version string comes off
# the network and is printed to the operator's terminal, so anything else
# (escape sequences, newlines, shell text) is refused, not shown.
_PUBLIC_VERSION_RE = re.compile(
    r"\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?", re.ASCII
)
_MAX_VERSION_LENGTH = 64


def _public_version(value: object) -> str | None:
    """Return *value* if it is a short PEP 440 public version, else None."""
    if not isinstance(value, str) or len(value) > _MAX_VERSION_LENGTH:
        return None
    return value if _PUBLIC_VERSION_RE.fullmatch(value) else None


def _resolve_current_version() -> str:
    try:
        return importlib.metadata.version("kew-cli")
    except importlib.metadata.PackageNotFoundError:
        from kew import __version__

        return __version__


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _install_fingerprint(version: str, direct_url: dict[str, Any] | None) -> str:
    import hashlib

    blob = version + "\0" + json.dumps(direct_url, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _cache_path(cache_dir: Path, fingerprint: str) -> Path:
    return cache_dir / f"staleness-{fingerprint}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("ts", 0)
    if not isinstance(ts, (int, float)):
        return None
    if time.time() - ts > CACHE_TTL_SECONDS:
        return None
    if "latest" not in data or "editable" not in data:
        return None  # an older cache format held rendered warnings; refetch
    return data


def _write_cache(path: Path, *, latest: str | None, editable: str | None) -> None:
    """Cache the inputs of the warnings, never the rendered text.

    A replay re-validates ``latest`` and rebuilds the warning, so a cache file
    cannot print a version the fetch would refuse.
    """
    import contextlib

    payload = {"ts": time.time(), "latest": latest, "editable": editable}
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Version check (PyPI JSON API)
# ---------------------------------------------------------------------------


def _fetch_latest_version() -> str | None:
    req = Request(_PYPI_LATEST_URL, headers={"Accept": "application/json"})  # noqa: S310 — PyPI JSON API, not arbitrary URL
    try:
        with urlopen(req, timeout=5) as resp:  # noqa: S310  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected — hardcoded PyPI JSON API URL
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — network errors must never block startup
        return None
    info = data.get("info") if isinstance(data, dict) else None
    return _public_version(info.get("version")) if isinstance(info, dict) else None


def _version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in v.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            break
    return tuple(parts)


def _version_lt(a: str, b: str) -> bool:
    ta, tb = _version_tuple(a), _version_tuple(b)
    max_len = max(len(ta), len(tb))
    return ta + (0,) * (max_len - len(ta)) < tb + (0,) * (max_len - len(tb))


# ---------------------------------------------------------------------------
# Editable-branch detection
# ---------------------------------------------------------------------------


def _get_direct_url() -> dict[str, Any] | None:
    try:
        dist = importlib.metadata.distribution("kew-cli")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        text = dist.read_text("direct_url.json")
    except FileNotFoundError:
        return None
    if text is None:
        return None
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except (ValueError, TypeError):
        return None


def _git_branch_at(path: str) -> str | None:
    try:
        result = run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_commits_behind(path: str) -> int | None:
    """Return commits in local ``origin/main`` that are absent from HEAD."""
    try:
        result = run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            cwd=path,
            capture=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    try:
        count = int(result.stdout.strip())
    except ValueError:
        return None
    return count if count >= 0 else None


def _editable_source_path(du: dict[str, Any] | None) -> str | None:
    if du is None:
        return None
    dir_info = du.get("dir_info", {})
    if not isinstance(dir_info, dict) or not dir_info.get("editable"):
        return None
    url = du.get("url", "")
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    return url.removeprefix("file://")


def _check_editable_branch() -> str | None:
    du = _get_direct_url()
    source_path = _editable_source_path(du)
    if source_path is None:
        return None
    try:
        branch = _git_branch_at(source_path)
    except OSError:
        return None
    if branch is None:
        return None
    behind = _git_commits_behind(source_path)
    if behind is None or behind == 0:
        return None
    branch_name = "detached HEAD" if branch == "HEAD" else branch
    branch_name = "".join(char for char in branch_name if char.isprintable())[:120]
    return (
        f"Editable install on branch '{branch_name}' is {behind} "
        f"commit{'s' if behind != 1 else ''} behind local origin/main — "
        f"update with: git -C {shlex.quote(source_path)} merge --ff-only origin/main"
    )


def check_editable_staleness() -> str | None:
    """Return an actionable warning when an editable source checkout is stale."""
    return _check_editable_branch()


# ---------------------------------------------------------------------------
# Repo-version skew (#433)
# ---------------------------------------------------------------------------


def _read_repo_kew_version() -> str | None:
    pyproject = Path("kew/pyproject.toml")
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = data.get("project", {}).get("version")
        return str(version) if isinstance(version, str) else None
    except Exception:  # noqa: BLE001
        return None


def _read_db_schema_version(db_path: Path) -> int | None:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        conn.close()
        return int(row["version"]) if row else None
    except Exception:  # noqa: BLE001
        return None


def check_orchestrator_skew(*, db_path: Path | None = None) -> list[str]:
    """Warn when the running kew is behind the target repo or schema.

    Never raises — all errors are swallowed.
    """
    warnings: list[str] = []
    try:
        current = _resolve_current_version()

        repo_version = _read_repo_kew_version()
        if repo_version is not None and _version_lt(current, repo_version):
            warnings.append(
                f"kew {current} is behind this repo's kew ({repo_version}). "
                f"Update: "
                f"{UPGRADE_HINT}"
            )

        if db_path is not None:
            from kew.run_store import CURRENT_SCHEMA_VERSION

            db_schema = _read_db_schema_version(db_path)
            if db_schema is not None and db_schema > CURRENT_SCHEMA_VERSION:
                warnings.append(
                    f"dispatch.db is schema v{db_schema}; this kew supports up "
                    f"to v{CURRENT_SCHEMA_VERSION}. "
                    f"Update kew before it hard-blocks: "
                    f"{UPGRADE_HINT}"
                )
    except Exception:  # noqa: BLE001 — must never break startup
        return []
    return warnings


# ---------------------------------------------------------------------------
# Governance config drift (#638)
# ---------------------------------------------------------------------------

# Governance sections whose local and committed copies can differ. ``budget``
# and ``breaker`` are read by the supervising process from the locally loaded
# config (uncommitted edits included), so the local value already governs this
# machine's dispatches; another machine or CI reads the committed file.
# ``security`` reaches the agent through its clone, which carries the
# committed file, so a local edit does not apply until it is committed.
_GOVERNANCE_SECTIONS = ("budget", "breaker", "security")

# Sections the supervising process enforces from the local config.
_LOCALLY_GOVERNED_SECTIONS = frozenset({"budget", "breaker"})


def _read_committed_config_text(config_path: Path) -> str | None:
    """Return the committed kew.toml text from origin/HEAD, or None if unavailable.

    Uses git show origin/HEAD:<path-relative-to-git-root>.  Falls back to
    origin/main and origin/master if origin/HEAD is not resolvable.  Never raises.
    """
    try:
        git_root_result = run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=config_path.parent,
            capture=True,
            check=False,
            timeout=5,
        )
        if git_root_result.returncode != 0:
            return None
        git_root = Path(git_root_result.stdout.strip())
        try:
            rel_path = config_path.resolve().relative_to(git_root.resolve())
        except ValueError:
            return None
        rel_str = rel_path.as_posix()

        for ref in ("origin/HEAD", "origin/main", "origin/master"):
            result = run(
                ["git", "show", f"{ref}:{rel_str}"],
                cwd=git_root,
                capture=True,
                check=False,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout
        return None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _extract_governance_fields(toml_text: str) -> dict[str, object]:
    """Parse toml_text and return a flat dict of governance field values.

    Only includes fields present in ``_GOVERNANCE_SECTIONS``.  Values absent
    from the TOML are not included — the caller compares only present keys.
    """
    try:
        data = tomllib.loads(toml_text)
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, object] = {}
    for section in _GOVERNANCE_SECTIONS:
        section_data = data.get(section)
        if not isinstance(section_data, dict):
            continue
        for key, value in section_data.items():
            result[f"{section}.{key}"] = value
    return result


def _active_override_fields(store: RunStore | None) -> set[str]:
    """Return the set of dotted field paths with an active config_overrides row.

    The runtime resolution order is env > DB > toml, so any field carrying an
    active DB override ignores *both* the local and committed toml value.  This
    is consulted the same way ``config._apply_db_overrides`` reads the layer.
    Never raises — a broken/absent store yields an empty set.
    """
    if store is None:
        return set()
    try:
        return {str(row["field"]) for row in store.list_config_overrides()}
    except Exception:  # noqa: BLE001 — override lookup must never break the check
        return set()


def _drift_warning(key: str, local_val: object, committed_val: object) -> str:
    """The operator-facing warning for one drifted governance field."""
    values = f"({key}: local {local_val!r} / committed {committed_val!r})"
    if key.split(".", 1)[0] in _LOCALLY_GOVERNED_SECTIONS:
        return (
            f"local kew.toml differs from committed config {values}. "
            f"The local value already governs this machine's dispatches; "
            f"it is not committed, so other machines and CI use the "
            f"committed one."
        )
    return (
        f"local kew.toml differs from committed config — "
        f"dispatch worktrees use the committed file {values}. "
        f"Commit kew.toml to apply this change to dispatched agents."
    )


def check_governance_config_drift(
    config_path: Path | None,
    *,
    store: RunStore | None = None,
) -> list[str]:
    """Warn when local kew.toml governance fields differ from the committed file.

    Compares local vs committed and warns on any mismatch in the budget,
    breaker, and security sections. The supervising process enforces
    ``budget`` and ``breaker`` from the local file, so a drift there already
    governs this machine's dispatches and the warning says that other machines
    and CI use the committed value. ``security`` reaches dispatch agents
    through their worktree, checked out from origin, so a local edit there does
    not apply until it is committed.

    Fields carrying an active ``config_overrides`` DB row are silently skipped:
    the DB layer wins over toml (env > DB > toml), so the effective value is
    identical for both the local and committed toml — no actionable drift.
    Pass *store* to consult that layer; without it the toml-only behaviour is
    unchanged.

    Never raises — all errors are swallowed.  Returns [] when there is nothing
    to warn about (no config file, no remote, no diff).
    """
    if config_path is None or not config_path.exists():
        return []
    try:
        local_text = config_path.read_text(encoding="utf-8")
        committed_text = _read_committed_config_text(config_path)
        if committed_text is None:
            return []

        local_fields = _extract_governance_fields(local_text)
        committed_fields = _extract_governance_fields(committed_text)
        overridden = _active_override_fields(store)

        warnings: list[str] = []
        all_keys = set(local_fields) | set(committed_fields)
        for key in sorted(all_keys):
            local_val = local_fields.get(key)
            committed_val = committed_fields.get(key)
            if local_val == committed_val:
                continue
            if key in overridden:
                continue  # DB value wins over both TOML files; no actionable drift
            warnings.append(_drift_warning(key, local_val, committed_val))
        return warnings
    except Exception:  # noqa: BLE001 — must never break startup
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _staleness_warnings(current: str, latest: object, editable: object) -> list[str]:
    warnings: list[str] = []
    release = _public_version(latest)
    if release is not None and _version_lt(current, release):
        warnings.append(
            f"kew {current} is behind the latest release ({release}). "
            f"Upgrade: "
            f"{UPGRADE_HINT}"
        )
    if isinstance(editable, str) and editable:
        warnings.append(editable)
    return warnings


def check_staleness(*, cache_dir: Path | None = None) -> list[str]:
    """Return a list of warning strings (empty = all good).

    Results are cached for CACHE_TTL_SECONDS.  Opt out by setting
    KEW_NO_UPDATE_CHECK to any non-empty value.
    Never raises — all errors are swallowed so staleness checks cannot break startup.
    """
    if os.environ.get(_OPT_OUT_ENV):
        return []

    try:
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "kew"

        current = _resolve_current_version()
        du = _get_direct_url()
        fp = _install_fingerprint(current, du)
        cp = _cache_path(cache_dir, fp)

        cached = _read_cache(cp)
        if cached is not None:
            return _staleness_warnings(current, cached["latest"], cached["editable"])

        latest = _fetch_latest_version()
        editable = _check_editable_branch()
        _write_cache(cp, latest=latest, editable=editable)
        return _staleness_warnings(current, latest, editable)
    except Exception:  # noqa: BLE001 — staleness must never break startup
        return []
