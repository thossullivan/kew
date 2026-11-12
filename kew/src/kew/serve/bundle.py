"""Web-bundle presence + freshness introspection for `kew serve`.

The SvelteKit SPA is built by `scripts/build_web.sh` and copied into
`src/kew/serve/web/`; that build also drops a `build-stamp.json` recording the
git sha and build timestamp (see `write_stamp`). This module reads that stamp so
`kew serve` can:

* serve a loud hint page (instead of a bare 404) when the bundle is absent, and
* warn — at startup and via a footer badge — when the checked-in `frontend/`
  source is newer than the deployed bundle (i.e. someone forgot to rebuild).

Stdlib-only: the import-linter contract forbids `kew.serve` importing the
stores/audit/orchestrator layers, and this module honours it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STAMP_FILENAME = "build-stamp.json"


@dataclass(frozen=True)
class BundleStatus:
    """Resolved state of the deployed web bundle relative to its source.

    `built_at` / `source_mtime` are unix timestamps (0.0 when unknown, e.g. an
    older bundle without a stamp, or a wheel install with no `frontend/` tree).
    """

    present: bool
    git_sha: str
    built_at: float
    source_mtime: float
    stale: bool


def read_stamp(static_dir: Path | None) -> dict[str, Any] | None:
    """Return the parsed `build-stamp.json`, or None if missing/unreadable."""
    if static_dir is None:
        return None
    stamp = Path(static_dir) / STAMP_FILENAME
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def newest_source_mtime(frontend_dir: Path | None) -> float:
    """Newest mtime under `frontend_dir` (source files), or 0.0 if unavailable.

    Used only from a source checkout; an installed wheel has no `frontend/`
    tree, so this returns 0.0 and staleness is never reported there.
    """
    if frontend_dir is None:
        return 0.0
    root = Path(frontend_dir)
    if not root.is_dir():
        return 0.0
    newest = 0.0
    for path in root.rglob("*"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest = mtime
    return newest


def evaluate_bundle(
    static_dir: Path | None, frontend_dir: Path | None = None
) -> BundleStatus:
    """Resolve bundle presence + freshness from disk (pure w.r.t. its inputs)."""
    present = static_dir is not None and Path(static_dir).is_dir()
    stamp = read_stamp(static_dir) if present else None
    git_sha = str(stamp.get("git_sha", "")) if stamp else ""
    try:
        built_at = float(stamp.get("built_at", 0.0)) if stamp else 0.0
    except (TypeError, ValueError):
        built_at = 0.0
    source_mtime = newest_source_mtime(frontend_dir) if present else 0.0
    # Staleness needs both a stamped build time and a readable source tree;
    # without either we cannot make the comparison, so we stay silent.
    stale = bool(present and built_at > 0.0 and source_mtime > built_at)
    return BundleStatus(
        present=present,
        git_sha=git_sha,
        built_at=built_at,
        source_mtime=source_mtime,
        stale=stale,
    )


def startup_warnings(status: BundleStatus) -> list[str]:
    """Operator-facing warning lines for the resolved bundle state (may be empty)."""
    if not status.present:
        return [
            "web UI bundle missing (src/kew/serve/web) — serving a hint page "
            "only. Build it: bash scripts/build_web.sh",
        ]
    if status.stale:
        return [
            "web UI bundle is stale — frontend/ source is newer than the "
            "deployed bundle. Rebuild: bash scripts/build_web.sh",
        ]
    return []


def missing_build_html() -> str:
    """Self-contained hint page shown when the SPA bundle is absent.

    No external assets: the bundle that would serve CSS/JS is exactly what is
    missing, so everything is inlined.
    """
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kew — web UI not built</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #0b0d10; color: #e6e8eb; display: grid; min-height: 100vh;
         place-items: center; padding: 2rem; }
  main { max-width: 40rem; }
  h1 { font-size: 1.4rem; margin: 0 0 0.75rem; }
  code, pre { background: #1a1e24; border-radius: 6px; }
  code { padding: 0.1rem 0.35rem; }
  pre { padding: 0.9rem 1rem; overflow-x: auto; }
  a { color: #7aa2f7; }
  .muted { color: #9aa5b1; }
</style>
</head>
<body>
<main>
  <h1>kew web UI is not built</h1>
  <p>The API is running, but the single-page app bundle
     (<code>src/kew/serve/web</code>) was not found, so there is nothing to
     render here.</p>
  <p>Build it from the repo root:</p>
  <pre>bash scripts/build_web.sh</pre>
  <p class="muted">Then restart <code>kew serve</code>. The JSON API under
     <code>/api</code> works regardless of the UI build.</p>
</main>
</body>
</html>"""
