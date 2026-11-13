"""Frontend dependencies for the governed reviewer, without network (#2559).

The review checkout carries no ``frontend/node_modules`` and the reviewer has
no network, so every UI review used to be read-only. This module gives it the
same dependencies the implementer installs, under three rules:

* What is installed is chosen by the BASE commit's ``frontend/package-lock.json``
  and never by the PR's: a PR could point a dependency at hostile code. A PR
  that changes the lockfile therefore gets no mount, and the prompt says why.
* The tree is built on the host side before the reviewer starts, in the review
  image (so native binaries match the container), with ``--ignore-scripts`` and
  no runner credential, and cached under the lockfile's sha256. The same lock
  is installed once, whichever review asks first.
* The reviewer gets it as a READ-ONLY bind at ``/work/frontend/node_modules``.
  Vite writes its cache to ``node_modules/.vite``, so the cached tree carries a
  ``.vite`` symlink to a writable directory the host creates in the checkout.

The decision (mount or not, and the prompt line) is taken in the dispatch from
git facts alone, so the attested prompt says the same thing whether or not the
install later succeeds; the prompt tells the reviewer how to read an absent
mount.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kew.sandbox import WORK_MOUNT_PATH, build_run_argv, resource_caps
from kew.subprocess_utils import run, run_bytes

if TYPE_CHECKING:
    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

FRONTEND_DIR = "frontend"
LOCKFILE = f"{FRONTEND_DIR}/package-lock.json"
MANIFEST = f"{FRONTEND_DIR}/package.json"
#: Where the reviewer sees the read-only tree.
CONTAINER_NODE_MODULES = f"{WORK_MOUNT_PATH}/{FRONTEND_DIR}/node_modules"
#: A writable directory in the checkout root that ``node_modules/.vite`` points
#: at; ``.kew-*`` names are the host's own files in a review checkout.
VITE_CACHE_DIRNAME = ".kew-vite-cache"
CONTAINER_VITE_CACHE = f"{WORK_MOUNT_PATH}/{VITE_CACHE_DIRNAME}"
#: The install: the lockfile exactly, and no package's lifecycle scripts.
NPM_CI_COMMAND = ("ci", "--ignore-scripts", "--no-audit", "--no-fund")
INSTALL_TIMEOUT_SECONDS = 900.0

ReadOnlyMount = tuple[Path, str]

_MOUNTED_SECTION = """

## Frontend acceptance

The host built `frontend/node_modules` from the BASE commit's
`frontend/package-lock.json` (sha256 `{sha}`) and mounted it read-only at
`/work/frontend/node_modules`. Run the frontend's own checks offline from
`/work/frontend`:

- `npx --offline vitest run <the test files this diff touches>`
- `npx --offline svelte-kit sync`, then
  `npx --offline svelte-check --tsconfig ./tsconfig.json`

Do not install or update anything. If `/work/frontend/node_modules` is absent or
empty, the host could not build it: say so in `acceptance_reason` and review
the frontend by reading. A READY on a frontend change rests on these commands
having run; say in your verdict which ones you ran.
"""

_LOCK_CHANGED_SECTION = """

## Frontend acceptance

This PR changes `frontend/package-lock.json`, so the host mounted no
`frontend/node_modules`: what is installed for a review is chosen by the base
commit's lockfile, never by the PR's own. The frontend's tests cannot run in
this review; review the frontend by reading and say so in `acceptance_reason`.
"""


@dataclass(frozen=True)
class FrontendReviewPlan:
    """Whether this round gets a mount, and what the prompt says about it."""

    lock_sha256: str | None
    """sha256 of the base lockfile, the cache key; None means no mount."""
    section: str


def _blob_id(checkout_dir: Path, ref: str, path: str) -> str | None:
    """The object id at ``ref:path`` when it is a blob, else None."""
    probe = run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}:{path}"],
        cwd=checkout_dir,
        check=False,
    )
    oid = probe.stdout.strip()
    if probe.returncode != 0 or not oid:
        return None
    kind = run(["git", "cat-file", "-t", oid], cwd=checkout_dir, check=False)
    return oid if kind.stdout.strip() == "blob" else None


def _blob_bytes(checkout_dir: Path, oid: str) -> bytes:
    shown = run_bytes(["git", "cat-file", "blob", oid], cwd=checkout_dir)
    if shown.returncode != 0:
        raise RuntimeError(f"git cat-file blob {oid} exited {shown.returncode}")
    return shown.stdout


def plan_frontend_review(
    checkout_dir: Path, base_ref: str, head_ref: str
) -> FrontendReviewPlan:
    """Decide the round's frontend dependencies from the pinned refs alone."""
    base_lock = _blob_id(checkout_dir, base_ref, LOCKFILE)
    if base_lock is None:
        return FrontendReviewPlan(None, "")
    if _blob_id(checkout_dir, head_ref, LOCKFILE) != base_lock:
        return FrontendReviewPlan(None, _LOCK_CHANGED_SECTION)
    sha = hashlib.sha256(_blob_bytes(checkout_dir, base_lock)).hexdigest()
    return FrontendReviewPlan(sha, _MOUNTED_SECTION.format(sha=sha))


def default_cache_root() -> Path:
    """The host cache of built trees, one directory per base-lockfile sha256."""
    return Path.home() / ".cache" / "kew" / "review-node-modules"


def prepare_frontend_mount(
    config: DispatchConfig,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    lock_sha256: str | None,
    base_ref: str,
    cache_root: Path | None = None,
) -> tuple[ReadOnlyMount, ...]:
    """The read-only mount for this review, building the cache when absent.

    Returns no mount, with the reason logged, whenever anything is off: the
    prompt already tells the reviewer to read an absent tree as "the host
    could not build it". :class:`~kew.review_environment.ProvisioningUnconfirmed`
    from the install container propagates, as it does for the Python
    environment: the review must not start beside a container nobody saw end.
    """
    if lock_sha256 is None:
        return ()
    if not _prepare_mount_point(Path(checkout_dir)):
        _logger.warning(
            "review: %s/node_modules in the checkout is not an empty directory "
            "the host can mount over; no frontend dependencies",
            FRONTEND_DIR,
        )
        return ()
    target = (cache_root or default_cache_root()) / lock_sha256
    if target.is_symlink():
        _logger.warning("review: frontend cache %s is a symlink; not mounted", target)
        return ()
    if not target.is_dir():
        from kew.review_environment import ProvisioningUnconfirmed

        try:
            refusal = _build_cache(
                config,
                image=image,
                runner=runner,
                checkout_dir=Path(checkout_dir),
                slug=slug,
                lock_sha256=lock_sha256,
                base_ref=base_ref,
                target=target,
            )
        except ProvisioningUnconfirmed:
            raise
        except Exception as exc:  # noqa: BLE001 — a degraded review, not a lost one
            refusal = f"the install did not complete ({exc})"
        if refusal is not None:
            _logger.warning(
                "review: frontend dependencies not built (%s); the reviewer "
                "reads the frontend without running it",
                refusal,
            )
            return ()
    return ((target.absolute(), CONTAINER_NODE_MODULES),)


def _prepare_mount_point(checkout: Path) -> bool:
    """Create the empty mount point and the writable Vite cache, or refuse.

    A head that made ``frontend`` or ``frontend/node_modules`` a symlink, or
    tracks files under ``node_modules``, gets no mount rather than a bind
    whose destination the PR chose.
    """
    frontend = checkout / FRONTEND_DIR
    if frontend.is_symlink() or not frontend.is_dir():
        return False
    mount_point = frontend / "node_modules"
    vite_cache = checkout / VITE_CACHE_DIRNAME
    for path in (mount_point, vite_cache):
        if path.is_symlink():
            return False
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            return False
        path.mkdir(exist_ok=True)
    return True


def _build_cache(
    config: DispatchConfig,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    lock_sha256: str,
    base_ref: str,
    target: Path,
) -> str | None:
    """``npm ci`` the base lockfile in *image* and publish it as *target*.

    Returns the refusal reason, else None. The tree is built in a scratch
    directory beside *target* and renamed into place, so a reader only ever
    sees a finished tree; two reviews building the same lock both succeed and
    the second rename loses harmlessly.
    """
    from kew.review_environment import _LaunchState, _run_bounded

    lock_oid = _blob_id(checkout_dir, base_ref, LOCKFILE)
    manifest_oid = _blob_id(checkout_dir, base_ref, MANIFEST)
    if lock_oid is None or manifest_oid is None:
        return f"the base commit has no {LOCKFILE} and {MANIFEST}"
    lock = _blob_bytes(checkout_dir, lock_oid)
    if hashlib.sha256(lock).hexdigest() != lock_sha256:
        return "the base lockfile no longer matches the planned hash"
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"build-{slug}-", dir=target.parent))
    launch = _LaunchState()
    try:
        (scratch / "package-lock.json").write_bytes(lock)
        (scratch / "package.json").write_bytes(_blob_bytes(checkout_dir, manifest_oid))
        argv = build_run_argv(
            runtime=config.sandbox.runtime,
            image=image,
            checkout_dir=scratch,
            slug=f"{slug}-node",
            network=config.sandbox.network,
            proxy_environment=config.sandbox.proxy_environment,
            runner=runner,
            entrypoint="npm",
            command=list(NPM_CI_COMMAND),
            # An explicit empty mapping: no runner credential reaches the install.
            credential_environment={},
            caps=resource_caps(config.sandbox),
        )
        installed = _run_bounded(
            argv,
            scratch,
            INSTALL_TIMEOUT_SECONDS,
            label="npm ci",
            launch=launch,
            proxy_environment=config.sandbox.proxy_environment,
        )
        if installed.exit_code != 0:
            return f"npm ci exited {installed.exit_code}"
        return _publish(scratch / "node_modules", target)
    finally:
        if not launch.uncertain:
            shutil.rmtree(scratch, ignore_errors=True)


def _publish(built: Path, target: Path) -> str | None:
    """Point ``.vite`` at the writable cache and rename the tree into place."""
    if built.is_symlink() or not built.is_dir():
        return "npm ci produced no node_modules directory"
    vite = built / ".vite"
    if vite.is_symlink() or vite.is_file():
        vite.unlink()
    elif vite.is_dir():
        shutil.rmtree(vite)
    vite.symlink_to(CONTAINER_VITE_CACHE)
    try:
        os.rename(built, target)
    except OSError:
        if not target.is_dir():
            raise
    return None
