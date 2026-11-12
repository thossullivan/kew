"""What may be applied into a host-owned clone, and what must not (W1.1 / D4).

The broker's shape is: the container extracts the agent's work into an artifact,
the host reads that artifact as opaque bytes, and the host applies it into a
clone the agent has never touched. This module is the gate in the middle. It
decides nothing about *how* to apply anything — it says which entries are
allowed to reach a repository the operator owns.

**Reject loudly, never silently drop.** That rule is enforced structurally
rather than by convention: a verdict with any rejection has no readable set of
approved entries — :meth:`FilterVerdict.approved` raises. A caller cannot filter
the bad paths out and publish the rest by accident, because the API gives it no
way to spell that.

The rejection list is a denylist, which this project is otherwise wary of. It is
sound here only because it sits behind an **allowlist of modes**: the sole
accepted entries are regular files at ``100644`` or ``100755``. Symlinks,
gitlinks, submodules, devices and fifos are not enumerated as attacks; they are
simply not the two things allowed. The name-based rules exist because ``.git``
must not be reachable *even as a regular file*.

**Publication scope** is the one place an entry leaves without a rejection:
untracked writes beneath a generated-cache directory (``__pycache__``,
``.mypy_cache`` and similar), and untracked dependency installs (a
``node_modules`` or ``.venv`` directory the destination tracks nothing in) are
not work and are not published. A cache entry leaves after every rule above has
passed. An install leaves after per-entry validation but before the flags and
the set-level rules, which hold or refuse the whole publication — it arrives as
thousands of paths that are then discarded, and a gate that fires over those
protects nothing.

**No callers yet, by construction** — spec node 4. Shadow mode (node 5) compares
this path against the legacy one before anything is flipped.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "FilterVerdict",
    "Flag",
    "ManifestEntry",
    "PublicationRefused",
    "ReviewRequired",
    "Rejection",
    "filter_artifact",
    "filter_manifest",
    "collision_refusals",
    "is_untracked_dependency_directory",
    "path_shape_refusal",
    "tracked_path_keys",
]

# The only two things a publication may contain: a regular file, executable or
# not. Everything else — 120000 symlink, 160000 gitlink, 040000 tree, and any
# device or fifo a packer might encode — falls outside this set and is refused
# without needing to be named as an attack.
MODE_FILE = "100644"
MODE_EXECUTABLE = "100755"
ALLOWED_MODES = frozenset({MODE_FILE, MODE_EXECUTABLE})

# git's own name for a mode we recognise, used only to make the refusal legible.
_MODE_NAMES = {
    "120000": "a symlink",
    "160000": "a gitlink (submodule)",
    "040000": "a directory entry",
    "100664": "a file with a non-canonical mode",
}

# The 8.3 short-name alias for ``.git`` on Windows filesystems (``GIT~1``).
# A checkout there resolves it to the real directory, so it is the same reach.
_SHORT_NAME_GIT = re.compile(r"^git~[0-9]+$")

# Code points HFS+ IGNORES when comparing filenames — so on macOS every one of
# these spellings of ".git" opens the real .git directory. Taken from git's own
# `next_hfs_char` (utf8.c), which backs `core.protectHFS`; keeping the same set
# means kew refuses exactly what git refuses rather than a set someone guessed.
_HFS_IGNORABLE = dict.fromkeys(
    [
        0x200C,  # zero width non-joiner
        0x200D,  # zero width joiner
        0x200E,  # left-to-right mark
        0x200F,  # right-to-left mark
        0x202A,  # left-to-right embedding
        0x202B,  # right-to-left embedding
        0x202C,  # pop directional formatting
        0x202D,  # left-to-right override
        0x202E,  # right-to-left override
        0x206A,  # inhibit symmetric swapping
        0x206B,  # activate symmetric swapping
        0x206C,  # inhibit arabic form shaping
        0x206D,  # activate arabic form shaping
        0x206E,  # national digit shapes
        0x206F,  # nominal digit shapes
        0xFEFF,  # zero width no-break space / BOM
    ]
)

# Paths that are not attacks but ARE code the repository executes later, with
# the repository's own credentials. They stop the publication for a human to
# look at rather than being refused outright.
#
# This list is POLICY, not a boundary. It cannot be complete — "everything a
# repository might execute" is open-ended — so it is a best-effort stop for a
# human, and nothing else in this module depends on it being exhaustive.
_REVIEW_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    ".circleci/",
    ".gitlab/",
    ".buildkite/",
)
# Editor and dev-container config, which the editor runs when the checkout is
# opened — tasks, launch configs, settings that name an interpreter or a
# formatter, a dev container's lifecycle commands — before any build or git
# command runs (#2012). Matched as whole path components at ANY depth, because
# a dev container or a project can be opened from a subdirectory.
_EDITOR_DIRS = frozenset({".vscode", ".idea", ".devcontainer"})
# The single-file form of `.devcontainer/devcontainer.json`.
_EDITOR_BASENAMES = frozenset({".devcontainer.json"})
_EDITOR_SUFFIXES = (".code-workspace",)
# Git hook directories, at any depth: husky's, and the `.githooks/` a
# `core.hooksPath` commonly names. A subproject keeps its own.
_HOOK_DIRS = frozenset({".husky", ".githooks"})
# Cargo's config, which every build reads for runners, rustc wrappers and
# linkers, in every ancestor of the build directory — so at any depth, under
# the legacy name and the current one.
_REVIEW_TOOL_TAILS = frozenset({(".cargo", "config.toml"), (".cargo", "config")})
# Makefile fragments an `include` pulls into every make run.
_REVIEW_SUFFIXES = (".mk",)
# `Dockerfile.dev`, `Dockerfile-dev`, `Dockerfile_prod`, `api.Dockerfile`,
# `Containerfile` — the same image recipe under the names docker and podman
# both accept. A source module or document named for one is not a recipe.
_IMAGE_RECIPE = re.compile(
    r"^(?:docker|container)file(?:[-_.].+)?$|\.(?:docker|container)file$"
)
_NOT_AN_IMAGE_RECIPE = re.compile(r"\.(?:py|md|rst|txt|ts|js|json)$")
_REVIEW_BASENAMES = frozenset(
    {
        ".pre-commit-config.yaml",
        ".pre-commit-config.yml",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        "jenkinsfile",
        ".travis.yml",
        "bitbucket-pipelines.yml",
        "conftest.py",
        "makefile",
        "package.json",
        "setup.py",
        "setup.cfg",
        # An in-tree build backend runs at install time.
        "pyproject.toml",
        ".envrc",
        # Build scripts, task runners and image recipes (#2012). An `.npmrc`
        # can name the registry and the scripts' shell for every install.
        "build.rs",
        "noxfile.py",
        "tox.ini",
        "justfile",
        ".justfile",
        ".npmrc",
        # Make's other default names, and task runners (#3350).
        "gnumakefile",
        "bsdmakefile",
        "taskfile.yml",
        "taskfile.yaml",
        # Package-manager config read on every install: yarn's can name a
        # `yarnPath` binary or plugins, pnpm's runs hooks on resolution.
        ".yarnrc.yml",
        ".yarnrc",
        ".pnpmfile.cjs",
        "bunfig.toml",
        # Git hook managers, whose config IS the hook commands.
        "lefthook.yml",
        "lefthook.yaml",
        ".lefthook.yml",
        ".lefthook.yaml",
        ".overcommit.yml",
        # Toolchain pins and tasks an activated shell or a build resolves.
        ".mise.toml",
        "mise.toml",
        ".tool-versions",
        "rust-toolchain.toml",
        "rust-toolchain",
        # pytest's `addopts` can load a plugin or a conftest from anywhere.
        "pytest.ini",
        # Compose builds images and mounts the host into containers.
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }
)

# The verification hosts' provisioning: the recipe that installs the toolchain
# on a fresh host, and the scanner pins (#2462) that CI, the image gate and
# that recipe all read from one file. Nothing here runs with the repository's
# credentials, but it decides what the supply-chain gates actually check and
# what lands on a machine that builds and scans images — and a moved pin or
# digest reads like a routine version bump in a diff nobody stops on.
_REVIEW_PROVISION_PREFIXES = ("kew/src/kew/hosts/provision/",)

# `filter`, `diff` and `merge` attributes each name a driver COMMAND resolved
# from git config. An agent that lands one has arranged for a program to run on
# the operator's machine later — on checkout, on diff, or during a merge,
# revert or cherry-pick. All three, because two of them being caught is what
# makes the third look safe.
# Win32 resolves these stems to DEVICES, with or without an extension, so a
# file named CON or NUL.txt is not a regular file there at all. kew's
# destination is a POSIX clone, so this is a hazard for a collaborator's later
# checkout rather than reach against this host — which is why it is a hold for
# a human rather than a refusal. Refusing outright would also reject ordinary
# names like aux.py, and over-rejection is what gets a gate turned off.
_DOS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

_DRIVER_ATTRIBUTE = re.compile(r"(?:^|\s)(?:filter|diff|merge)\s*=", re.MULTILINE)

# git ignores a line whose first non-blank character is '#'. Scanning it anyway
# rejects a file that does nothing — and over-rejection is how a gate gets
# turned off.
_GITATTRIBUTES_COMMENT = re.compile(r"^\s*#.*$", re.MULTILINE)

# These directories contain local analysis output, not source work. The list is
# deliberately exact and small. It is publication scope, not gitignore policy.
_GENERATED_CACHE_DIRECTORIES = frozenset(
    {
        ".mypy_cache",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
        ".import_linter_cache",
    }
)

# Directories a package manager fills from a lockfile (`npm ci`, `uv sync`).
# They hold symlinks (`node_modules/.bin/*`, `.venv/bin/python`) that no
# publication can carry, and none of it is work. Also publication scope, not
# gitignore policy, and exact: only a DIRECTORY with one of these names that
# the base tracks nothing at or beneath is outside scope. The host walker does
# not descend into one (kew.publication_extract), and the scope step below
# drops any untracked entry that still arrives from beneath one.
_DEPENDENCY_INSTALL_DIRECTORIES = frozenset({"node_modules", ".venv"})


@dataclass(frozen=True)
class ManifestEntry:
    """One file the container claims the agent produced.

    ``sha256`` is the host's hash over the artifact bytes, not the container's
    claim about them — this module never verifies it, it only carries it so a
    rejection can name the entry unambiguously.
    """

    path: str
    mode: str
    sha256: str = ""


@dataclass(frozen=True)
class Rejection:
    """A refusal. ``rule`` is stable and machine-readable; ``detail`` is prose."""

    path: str
    rule: str
    detail: str


@dataclass(frozen=True)
class Flag:
    """Not a refusal — a stop for human judgement."""

    path: str
    reason: str


class ReviewRequired(Exception):
    """Raised when a flagged publication is read without acknowledging the flags.

    Flags used to be advisory: ``needs_human`` reported them and ``approved()``
    handed the entries back anyway, so a caller reaching for the obvious
    accessor got ``.github/workflows/pwn.yml`` with no human in the loop. A
    hold that the API does not enforce is a comment.
    """

    def __init__(self, flags: tuple[Flag, ...]) -> None:
        self.flags = flags
        listed = "; ".join(f"{f.path}: {f.reason}" for f in flags[:5])
        more = "" if len(flags) <= 5 else f" (and {len(flags) - 5} more)"
        super().__init__(f"publication needs human review — {listed}{more}")


class PublicationRefused(Exception):
    """Raised when approved entries are read from a verdict that refused."""

    def __init__(self, rejections: tuple[Rejection, ...]) -> None:
        self.rejections = rejections
        listed = "; ".join(f"{r.path}: {r.detail}" for r in rejections[:5])
        more = "" if len(rejections) <= 5 else f" (and {len(rejections) - 5} more)"
        super().__init__(f"publication refused — {listed}{more}")


@dataclass(frozen=True)
class FilterVerdict:
    """The outcome of filtering one manifest.

    There is deliberately no attribute holding "the entries that passed". The
    only way to read them is :meth:`approved`, which refuses when anything was
    rejected — so "drop the bad ones and publish the rest" cannot be written by
    accident.
    """

    _approved: tuple[ManifestEntry, ...] = field(default_factory=tuple)
    _deletions: tuple[str, ...] = field(default_factory=tuple)
    rejections: tuple[Rejection, ...] = field(default_factory=tuple)
    flags: tuple[Flag, ...] = field(default_factory=tuple)

    @property
    def refused(self) -> bool:
        return bool(self.rejections)

    @property
    def needs_human(self) -> bool:
        """Nothing is wrong, but something here executes later. Ask a person."""
        return bool(self.flags)

    @property
    def review_required(self) -> bool:
        """Work a person can clear — never a refusal, which nobody can (I13).

        The human-review policy reads this rather than :attr:`needs_human`
        because the two answers diverge on a refused-and-flagged verdict. A
        rejection is not a heavier flag: no approver, allowlist entry or
        threshold releases it, so reporting one as "needs review" would offer
        a person a decision they are not allowed to make.
        """
        return bool(self.flags) and not self.rejections

    def review_reasons(self) -> tuple[str, ...]:
        """One stable ``path: reason`` line per flag, oldest-first."""
        return tuple(f"{flag.path}: {flag.reason}" for flag in self.flags)

    def refusal_reasons(self) -> tuple[str, ...]:
        """One stable ``path: detail`` line per rejection, oldest-first."""
        return tuple(f"{r.path}: {r.detail}" for r in self.rejections)

    def _gate(self, *, reviewed: bool) -> None:
        """The one place either hold is enforced, for both accessors."""
        if self.rejections:
            raise PublicationRefused(self.rejections)
        if self.flags and not reviewed:
            raise ReviewRequired(self.flags)

    def approved(self, *, reviewed: bool = False) -> tuple[ManifestEntry, ...]:
        """The entries safe to apply. Raises if ANY entry was rejected.

        A partial publication is not a safe subset of a rejected one: the
        rejected entry is evidence about the agent that produced all of them.

        Also raises :class:`ReviewRequired` while anything is flagged. Passing
        ``reviewed=True`` is the caller stating that a person looked — it has
        to be typed, so that releasing a hold is a deliberate act rather than
        the default behaviour of the convenient accessor.
        """
        self._gate(reviewed=reviewed)
        return self._approved

    def deletions(self, *, reviewed: bool = False) -> tuple[str, ...]:
        """The paths safe to remove. Raises on the same terms as :meth:`approved`.

        A deletion is not the harmless direction. ``rm`` on an attacker-chosen
        path removes the operator's file just as surely as a write overwrites
        it, and a path through a symlinked ancestor leaves the destination
        while pointing somewhere else entirely — so these carry exactly the
        same rules as an entry.
        """
        self._gate(reviewed=reviewed)
        return self._deletions


def _normalize(component: str) -> str:
    """A path component reduced to what a filesystem will actually match.

    Four reductions, each for a filesystem that does it for us:

    * **NFKC** folds the compatibility forms — a fullwidth ``ｇ`` is ``g`` to a
      normalising volume.
    * **HFS-ignorable code points are removed.** HFS+ omits these entirely when
      comparing filenames, so ``.gi<U+200C>t`` *is* ``.git`` on macOS. This is
      the whole reason git ships ``core.protectHFS`` (on by default there), and
      macOS is this project's primary platform.
    * **casefold** handles case-insensitive volumes.
    * **trailing dots and spaces** are stripped, because Windows drops them —
      ``.git.`` and ``.git`` name one directory there.
    """
    folded = unicodedata.normalize("NFKC", component)
    stripped = folded.translate(_HFS_IGNORABLE)
    # Normalize AGAIN: deleting an ignorable can leave characters adjacent that
    # were not before, and those may now compose. `a<U+200C><combining acute>`
    # becomes the DECOMPOSED `a<combining acute>`, which is a different string
    # from the precomposed `á` — while HFS, which ignores U+200C and decomposes
    # canonically, considers them one file. One pass is not a fixed point.
    recomposed = unicodedata.normalize("NFKC", stripped)
    return recomposed.casefold().rstrip(". ")


def _is_git_component(component: str) -> bool:
    normalized = _normalize(component)
    return normalized == ".git" or bool(_SHORT_NAME_GIT.match(normalized))


def _control_characters(path: str) -> bool:
    return any(ch < " " or ch == "\x7f" for ch in path)


def _unencodable(path: str) -> bool:
    """True if the path cannot be encoded as UTF-8.

    JSON permits lone surrogates, so a manifest can carry a ``str`` Python
    holds happily and every filesystem call rejects — turning what should be
    a refusal into a UnicodeEncodeError somewhere downstream. Deciding it
    here keeps the failure a refusal.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _check_path_text(path: str) -> Rejection | None:
    """Refusals decided by the path as a STRING, before it means anything."""
    if not path or path != path.strip():
        return Rejection(path, "empty_or_padded_path", "path is empty or padded")
    if _control_characters(path):
        return Rejection(path, "control_characters", "path contains control characters")
    if _unencodable(path):
        return Rejection(path, "unencodable", "path is not valid UTF-8")
    if path.startswith("/"):
        return Rejection(path, "absolute_path", "path is absolute")
    if "\\" in path:
        # A backslash is a legal character in a POSIX filename and a separator
        # on Windows, so the same manifest would mean two different trees.
        return Rejection(path, "backslash", "path contains a backslash separator")
    if ":" in path:
        # Same class as the backslash, and the reason the absolute-path check
        # cannot be a drive-letter pattern: `C:file` is DRIVE-RELATIVE on
        # Windows (no separator, so `^[A-Za-z]:[\\/]` misses it), and
        # `notes.txt:hidden` is an NTFS alternate data stream — content that
        # does not appear in an ordinary listing of `notes.txt`. A colon is
        # legal in a POSIX name, so refusing it outright is what keeps one
        # manifest from meaning two different trees.
        return Rejection(path, "colon", "path contains a colon")
    return None


_TRAVERSAL_COMPONENTS = frozenset({"", ".", ".."})


def _traverses(component: str) -> bool:
    """True if this component is a traversal, RAW or after normalization.

    Checking only the raw form was a gap: NFKC maps FULLWIDTH FULL STOP to
    ``.``, so ``．．`` is not ``..`` to a string comparison but is afterwards —
    and every other rule here already compares normalized. A component that
    means one thing to this check and another to the rest of the module is the
    ambiguity the whole filter exists to refuse, whether or not a particular
    filesystem would act on it.
    """
    return component in _TRAVERSAL_COMPONENTS or _normalize(component) in (
        _TRAVERSAL_COMPONENTS
    )


def _hides_a_separator(component: str) -> bool:
    """True if normalization turns this single component into several.

    NFKC maps FULLWIDTH SOLIDUS to ``/`` and FULLWIDTH REVERSE SOLIDUS to
    ``\\``. Such a component is one path segment to ``split("/")`` and two to
    anything that normalizes first, so the manifest would not describe one
    tree. git carries ``core.protectHFS``/``protectNTFS`` for this family of
    mappings; refusing is cheaper than modelling them.
    """
    folded = unicodedata.normalize("NFKC", component)
    return "/" in folded or "\\" in folded


def _check_path_components(path: str) -> Rejection | None:
    """Refusals decided by what the path's components NAME."""
    components = path.split("/")
    if any(_traverses(c) for c in components):
        return Rejection(path, "traversal", "path has an empty, '.' or '..' component")
    if any(_hides_a_separator(c) for c in components):
        return Rejection(
            path, "hidden_separator", "a component becomes a separator when normalized"
        )
    if any(_is_git_component(c) for c in components):
        return Rejection(path, "dot_git", "path reaches a .git component at some depth")
    return None


def _check_shape(path: str) -> Rejection | None:
    """Refusals that depend only on the path string.

    Split in two because the component rules are meaningless until the string
    rules have passed — an absolute or backslash-separated path does not have
    components you can reason about.
    """
    return _check_path_text(path) or _check_path_components(path)


def path_shape_refusal(path: str) -> Rejection | None:
    """The filter's string and component rules for one path, as a writer asks.

    For a caller that writes a path onto a filesystem itself: a padded,
    absolute, traversing or ``.git``-reaching name is refused by the same rule
    the filter applies, rather than by a second copy of it.
    """
    return _check_shape(path)


def _check_mode(
    entry: ManifestEntry, *, existing_modes: Mapping[str, str]
) -> Rejection | None:
    """Refusals that depend on the entry's mode.

    The allowlist is the whole defence; the ``_MODE_NAMES`` lookup only makes
    the message legible when the mode happens to be one git itself uses.
    """
    if entry.mode not in ALLOWED_MODES:
        named = _MODE_NAMES.get(entry.mode, "not a regular file")
        return Rejection(
            entry.path, "mode_not_allowed", f"mode {entry.mode} is {named}"
        )
    # Keyed the SAME way collisions are. `existing_modes` and
    # `destination_paths` both describe the destination, so comparing one by
    # exact string and the other by folded name left a hole: a caller that
    # passes modes but not paths got no escalation check at all for a
    # case-differing name, because the collision rule that would have caught it
    # never ran.
    was = existing_modes.get(_collision_key(entry.path))
    if was == MODE_FILE and entry.mode == MODE_EXECUTABLE:
        # Making an existing tracked file executable is how a repo's own script
        # becomes runnable without the diff showing any content change.
        return Rejection(
            entry.path,
            "mode_escalation",
            "file became executable (100644 -> 100755)",
        )
    return None


def _check_gitattributes(
    entry: ManifestEntry, read_text: Callable[[str], str] | None
) -> Rejection | None:
    """A ``.gitattributes`` may not introduce a ``filter`` or ``diff`` driver.

    Those name a command git runs on checkout. With no reader supplied the
    content cannot be inspected — so the entry is REFUSED rather than assumed
    innocent, because "we did not look" must never read as "we looked and it
    was fine".
    """
    if posixpath.basename(_normalize(entry.path)) != ".gitattributes":
        return None
    if read_text is None:
        return Rejection(
            entry.path,
            "gitattributes_unreadable",
            "cannot inspect .gitattributes for filter/diff drivers",
        )
    try:
        content = read_text(entry.path)
    except Exception as exc:  # noqa: BLE001 — any read failure is a refusal
        return Rejection(
            entry.path, "gitattributes_unreadable", f"could not be read: {exc}"
        )
    if _DRIVER_ATTRIBUTE.search(_GITATTRIBUTES_COMMENT.sub("", content)):
        return Rejection(
            entry.path,
            "gitattributes_driver",
            "introduces a filter/diff driver (runs a command on checkout)",
        )
    return None


def _collision_key(path: str) -> str:
    return "/".join(_normalize(c) for c in path.split("/"))


def _within_manifest_collision(path: str, prior: str) -> Rejection:
    """Two manifest entries that resolve to one file. Which one wins is undecided.

    The exact-duplicate case is NOT the harmless one: two entries naming the
    same path carry different content, and the apply step would write both, so
    the result depends on iteration order rather than on anything the manifest
    states.
    """
    if prior == path:
        return Rejection(path, "duplicate_path", "appears more than once")
    return Rejection(
        path, "name_collision", f"collides with {prior!r} on a case-folding filesystem"
    )


def _ancestor_keys(path: str) -> list[str]:
    """Every proper directory prefix of *path*, folded."""
    parts = _collision_key(path).split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _check_prefix_conflicts(
    paths: Iterable[str], file_keys: set[str]
) -> list[Rejection]:
    """An entry whose PARENT is already a file, not a directory.

    Two ways this bites, and the dangerous one is not obvious:

    * The destination tracks a symlink ``admin -> .git``. Nothing in
      ``admin/hooks/pre-commit`` contains ``.git``, every component is clean,
      and the mode is a regular file — but the write follows ``admin`` and
      lands in the repository's own hooks directory. The path rules cannot see
      this, because the ``.git`` is in the *destination*, not in the name.
    * One manifest carries both ``a`` and ``a/b``. No filesystem can hold ``a``
      as a file and a directory at once, so the outcome depends on apply order.

    Both are the same question — is any ancestor of this path already a file? —
    so they are one rule over one set of known file names.
    """
    rejections: list[Rejection] = []
    for path in paths:
        for ancestor in _ancestor_keys(path):
            if ancestor in file_keys:
                rejections.append(
                    Rejection(
                        path,
                        "prefix_is_a_file",
                        f"{ancestor!r} is a file, so this path writes THROUGH it",
                    )
                )
                break
    return rejections


def _check_collisions(
    operations: Iterable[tuple[str, bool]], destination_paths: Iterable[str]
) -> list[Rejection]:
    """Two names that a filesystem cannot tell apart.

    ``README.md`` and ``readme.md`` are distinct to git and the same file on a
    case-insensitive volume, so applying both means one silently overwrites the
    other — and which one wins is not something the manifest decides.

    An entry matching a destination path EXACTLY is an ordinary update and the
    common case; only a case- or normalization-differing match is a collision.
    """
    destination = {_collision_key(p): p for p in destination_paths}
    seen: dict[str, tuple[str, bool]] = {}
    rejections: list[Rejection] = []
    for path, writes in operations:
        key = _collision_key(path)
        prior = seen.get(key)
        if prior is not None:
            prior_path, prior_writes = prior
            if prior_writes != writes:
                # One manifest both writing and removing a path: which lands
                # depends on the order the applier happens to use, and the
                # manifest does not say.
                rejections.append(
                    Rejection(
                        path,
                        "written_and_deleted",
                        f"is also {'written' if prior_writes else 'deleted'} "
                        "by this manifest",
                    )
                )
            else:
                rejections.append(_within_manifest_collision(path, prior_path))
            continue
        seen[key] = (path, writes)
        existing = destination.get(key)
        if existing is not None and existing != path:
            rejections.append(
                Rejection(
                    path,
                    "name_collision",
                    f"collides with existing {existing!r} on a case-folding filesystem",
                )
            )
    return rejections


def collision_refusals(
    operations: Iterable[tuple[str, bool]], destination_paths: Iterable[str]
) -> list[Rejection]:
    """:func:`_check_collisions` for a caller outside the filter.

    *operations* are ``(path, writes)`` pairs; *destination_paths* is every path
    the destination already tracks. The placed-artifact writer runs this over a
    whole tree difference before it writes a byte (#2591).
    """
    return _check_collisions(operations, destination_paths)


def _fold_destination_modes(existing_modes: Mapping[str, str]) -> dict[str, str]:
    """Re-key the destination's modes by folded name, once per call.

    Where two destination paths fold together (a case-sensitive checkout can
    track both ``README.md`` and ``readme.md``), the NON-executable mode wins:
    it is the one that makes an escalation detectable, and guessing wrong in
    the other direction would silently drop the check.
    """
    folded: dict[str, str] = {}
    for path, mode in existing_modes.items():
        key = _collision_key(path)
        if folded.get(key) != MODE_FILE:
            folded[key] = mode
    return folded


def _destination_keys(
    existing_modes: Mapping[str, str], destination_paths: Iterable[str]
) -> set[str]:
    """Return normalized destination names from both trusted inputs."""
    return {_collision_key(path) for path in destination_paths} | set(existing_modes)


def _is_untracked_generated_cache(path: str, destination_keys: set[str]) -> bool:
    """Return true only for an untracked write beneath one reserved cache."""
    return _collision_key(path) not in destination_keys and any(
        part in _GENERATED_CACHE_DIRECTORIES for part in path.split("/")[:-1]
    )


def tracked_path_keys(paths: Iterable[str]) -> frozenset[str]:
    """Folded keys of every tracked path and of every directory above one.

    A directory is tracked when anything at or beneath it is. The folding is
    :func:`_collision_key`'s, so a case or normalisation variant of a tracked
    directory counts as tracked; over-matching only means a directory is
    walked and validated as ordinary content.
    """
    return _with_ancestor_keys(_collision_key(path) for path in paths)


def _with_ancestor_keys(keys: Iterable[str]) -> frozenset[str]:
    """Already-folded *keys* plus every directory key above each one."""
    expanded: set[str] = set()
    for key in keys:
        parts = key.split("/")
        expanded.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return frozenset(expanded)


def is_untracked_dependency_directory(path: str, tracked: frozenset[str]) -> bool:
    """True when *path* names a dependency install the base tracks nothing in.

    The last segment must match a reserved name exactly, as the cache rule
    does. Whether *path* is really a directory is the caller's question: the
    host walker asks ``lstat``, so a symlink or file with this name is never
    skipped.
    """
    return (
        path.rsplit("/", 1)[-1] in _DEPENDENCY_INSTALL_DIRECTORIES
        and _collision_key(path) not in tracked
    )


def _is_beneath_untracked_dependency_directory(
    path: str, tracked: frozenset[str]
) -> bool:
    parts = path.split("/")
    return any(
        is_untracked_dependency_directory("/".join(parts[:depth]), tracked)
        for depth in range(1, len(parts))
    )


def _is_untracked_worktree_state(path: str, destination_keys: set[str]) -> bool:
    """Return true for an untracked write beneath any .worktrees segment."""
    return _collision_key(path) not in destination_keys and any(
        _normalize(part) == ".worktrees" for part in path.split("/")[:-1]
    )


def _reject_untracked_worktree_state(
    entries: Iterable[ManifestEntry], destination_keys: set[str]
) -> list[Rejection]:
    """Refuse untracked writes beneath any .worktrees segment."""
    return [
        Rejection(
            entry.path,
            "untracked_worktree_state",
            "untracked write reaches a .worktrees directory",
        )
        for entry in entries
        if _is_untracked_worktree_state(entry.path, destination_keys)
    ]


def _remove_untracked_installs(
    entries: Iterable[ManifestEntry], destination_keys: set[str]
) -> list[ManifestEntry]:
    """Remove entries beneath a dependency install the destination tracks nothing in.

    Dropped by DIRECTORY, as the host walker skips one: any entry beneath a
    ``node_modules`` or ``.venv`` with nothing tracked at or beneath it. A
    tracked install directory stays ordinary content, untracked additions
    included.

    This half of publication scope leaves BEFORE the set-level rules and the
    review flags rather than after them (#2105). An install arrives whole: every
    package carries its own ``package.json``, so a flag per package would hold
    the publication for a person, and any two of its files whose names fold
    together would refuse it — over paths this step then discards. Per-entry
    validation has already run and still refuses, because a symlink or a driver
    ``.gitattributes`` reaching the filter is evidence about the producer rather
    than routine install output.
    """
    tracked = _with_ancestor_keys(destination_keys)
    return [
        entry
        for entry in entries
        if not _is_beneath_untracked_dependency_directory(entry.path, tracked)
    ]


def _remove_untracked_caches(
    entries: Iterable[ManifestEntry], destination_keys: set[str]
) -> list[ManifestEntry]:
    """Remove untracked writes beneath a reserved generated-cache directory.

    Dropped when the ENTRY itself is untracked, and last of all — unlike an
    install, which leaves earlier. A cache holds one run's analysis output
    rather than a vendored tree, so a file there with a CI-shaped name or a
    driver ``.gitattributes`` is a signal worth a person's attention, not a
    gate firing over routine bulk.
    """
    return [
        entry
        for entry in entries
        if not _is_untracked_generated_cache(entry.path, destination_keys)
    ]


def _flag_for(path: str, *, writes: bool) -> Flag | None:
    normalized = _collision_key(path)
    basename = posixpath.basename(normalized)
    if not writes and basename == ".gitattributes":
        # REMOVING one can activate a driver rather than introduce it: a nested
        # .gitattributes may unset an attribute a parent or global file sets, so
        # deleting it lets the inherited `filter`/`diff`/`merge` command apply
        # with no new content anywhere in the manifest.
        return Flag(path, "removing it can activate an inherited attribute driver")
    if _DOS_DEVICES & {basename, basename.split(".", 1)[0]}:
        return Flag(path, "a Windows reserved device name, not a file on Win32")
    reason = _protected_class(normalized)
    return None if reason is None else Flag(path, reason)


def _protected_class(normalized: str) -> str | None:
    """Why a folded path is code something runs later, or None."""
    parts = normalized.split("/")
    if normalized.startswith(_REVIEW_PREFIXES):
        return "CI workflow code, which runs with repository secrets"
    if normalized.startswith(_REVIEW_PROVISION_PREFIXES):
        return "verification-host provisioning and scanner pins"
    if not _HOOK_DIRS.isdisjoint(parts[:-1]):
        return "git hook, which runs on the next local git command"
    if _is_editor_config(parts):
        return (
            "runs when the checkout is opened in an editor; review it with "
            "`git show` before opening the checkout"
        )
    if _is_executed_tool_config(parts):
        return "executed by common local or build tooling"
    return None


def _is_editor_config(parts: list[str]) -> bool:
    """Whether folded path components are editor or dev-container config."""
    name = parts[-1]
    if name in _EDITOR_BASENAMES or name.endswith(_EDITOR_SUFFIXES):
        return True
    return not _EDITOR_DIRS.isdisjoint(parts[:-1])


def _is_executed_tool_config(parts: list[str]) -> bool:
    """Whether folded path components are build or tool config, at any depth."""
    name = parts[-1]
    if name in _REVIEW_BASENAMES or name.endswith(_REVIEW_SUFFIXES):
        return True
    if tuple(parts[-2:]) in _REVIEW_TOOL_TAILS:
        return True
    return bool(_IMAGE_RECIPE.search(name)) and not _NOT_AN_IMAGE_RECIPE.search(name)


def protected_path_reason(path: str) -> str | None:
    """Why writing *path* is in a protected publication class, or None.

    The publication filter's own human-review classes, read by ``kew drive``'s
    standing merge rule (#2618) so a path that stops a publication for a person
    can never be merged without one either.
    """
    flag = _flag_for(path, writes=True)
    return None if flag is None else flag.reason


def _already_in_merged_base(
    entry: ManifestEntry | None,
    merged_base_digest: Callable[[str], str | None] | None,
) -> bool:
    """Whether this write is byte-identical to the merged base's own version.

    Those bytes are already in the repository and already run there, so the
    write is not this publication's authorship — it is what merging the base
    into the checkout brought along (#2362). An entry with no digest (a caller
    that builds :class:`ManifestEntry` without one) is never exempt: an unknown
    answer must read like "different", never like "the base already has it".
    """
    if entry is None or merged_base_digest is None or not entry.sha256:
        return False
    return merged_base_digest(entry.path) == entry.sha256


def _review_flags(
    operations: Sequence[tuple[str, bool]],
    writes_by_path: Mapping[str, ManifestEntry],
    merged_base_digest: Callable[[str], str | None] | None,
) -> list[Flag]:
    """The human-review stops this operation set earns.

    A write the merged base already carries is skipped. A REMOVAL never is:
    "the base does not have this path either" and "nobody could tell us" are
    the same answer from that reader, and the safe reading of either is to ask
    a person.
    """
    flags: list[Flag] = []
    for path, writes in operations:
        flag = _flag_for(path, writes=writes)
        if flag is None:
            continue
        if writes and _already_in_merged_base(
            writes_by_path.get(path), merged_base_digest
        ):
            continue
        flags.append(flag)
    return flags


def _screen_writes(
    entries: Iterable[ManifestEntry],
    *,
    existing_modes: Mapping[str, str],
    read_text: Callable[[str], str] | None,
) -> tuple[list[ManifestEntry], list[Rejection]]:
    """Per-entry rules for written files: path shape, mode, and content."""
    approved: list[ManifestEntry] = []
    rejections: list[Rejection] = []
    for entry in entries:
        rejection = (
            _check_shape(entry.path)
            or _check_mode(entry, existing_modes=existing_modes)
            or _check_gitattributes(entry, read_text)
        )
        if rejection is None:
            approved.append(entry)
        else:
            rejections.append(rejection)
    return approved, rejections


def _screen_deletions(deletions: Iterable[str]) -> tuple[list[str], list[Rejection]]:
    """Per-operation rules for removals.

    A removal carries no new mode and no new content, so only the PATH rules
    apply here. Everything set-level — collisions, ancestors — is shared with
    writes rather than duplicated, which is the whole point of the split.
    """
    approved: list[str] = []
    rejections: list[Rejection] = []
    for path in deletions:
        rejection = _check_shape(path)
        if rejection is None:
            approved.append(path)
        else:
            rejections.append(rejection)
    return approved, rejections


def filter_manifest(
    entries: Iterable[ManifestEntry],
    *,
    deletions: Iterable[str],
    existing_modes: Mapping[str, str],
    destination_paths: Iterable[str],
    read_text: Callable[[str], str] | None = None,
    merged_base_digest: Callable[[str], str | None] | None = None,
) -> FilterVerdict:
    """Decide which operations may reach a host-owned clone.

    Every input here is REQUIRED, deliberately. Each one silently weakens a
    different rule when it is left out, and all of them defaulted before:
    omitting ``deletions`` dropped every removal the manifest asked for,
    omitting ``destination_paths`` disabled the collision and ancestor-symlink
    checks, and omitting ``existing_modes`` disabled mode-escalation. A caller
    got a clean verdict and less checking than it thought, which is exactly the
    silent-weakening this module exists to prevent. Pass ``{}`` / ``()`` to say
    "there is genuinely nothing here" — but say it.

    *existing_modes* and *destination_paths* describe the DESTINATION and must
    come from the host's own clone, never from anything the agent could write,
    or it would be answering its own question.

    *merged_base_digest* answers "what does the base this checkout merged hold
    at this path?", and only the REVIEW FLAGS consult it. A checkout cut from a
    branch head that then merged its base publishes every path the base moved
    in between, so the protected-path stop fired over ``.github/workflows/``
    edits that were already merged upstream and nobody had authored here
    (#2362: three remediation runs deferred and re-dispatched over main's own
    CI workflow). It must likewise come from the host's clone of the remote.
    ``None`` — the default, and every non-review publication — flags exactly as
    before. Nothing about a REJECTION consults it: no base content makes a
    symlink, a mode escalation or a ``.git`` path acceptable.

    *read_text* supplies artifact content for the few entries whose bytes must
    be inspected. Its absence is not permissive: an entry that cannot be
    inspected is refused.

    Writes and deletions share every SET-level rule. They are handled as one
    list of operations rather than two, because the alternative is two columns
    of rules kept level by hand — and a rule that reached one operation and not
    the other is precisely how a deletion came to bypass the ancestor-symlink
    defence.
    """
    entries = list(entries)
    deletions = list(deletions)
    destination_paths = list(destination_paths)
    modes = _fold_destination_modes(existing_modes)

    approved, rejections = _screen_writes(
        entries, existing_modes=modes, read_text=read_text
    )
    approved_deletions, deletion_rejections = _screen_deletions(deletions)
    rejections.extend(deletion_rejections)

    destination_keys = _destination_keys(modes, destination_paths)
    rejections.extend(_reject_untracked_worktree_state(approved, destination_keys))

    # An untracked dependency install leaves scope HERE, before the flags and
    # the set-level rules, because those hold or refuse the WHOLE publication
    # and none of these paths reaches the destination anyway.
    approved = _remove_untracked_installs(approved, destination_keys)

    operations = [(e.path, True) for e in approved]
    operations += [(path, False) for path in approved_deletions]
    flags = _review_flags(
        operations, {entry.path: entry for entry in approved}, merged_base_digest
    )

    # Collisions and prefix conflicts are properties of the SET, so they run
    # over every surviving operation — writes and removals alike.
    rejections.extend(_check_collisions(operations, destination_paths))
    # Every name known to be a FILE rather than a directory: the destination's
    # tracked paths (a symlink among them is exactly the dangerous case) and
    # the manifest's own writes.
    # A destination file this same artifact deletes is no longer a file for
    # the writes beneath it (a file replaced by a directory), and a deletion
    # beneath a path this artifact now writes as a file is the inverse
    # replacement; both are what the contained producer reports (round 6).
    deleted_keys = {_collision_key(p) for p in approved_deletions}
    base_file_keys = (
        {_collision_key(p) for p in destination_paths} | set(modes)
    ) - deleted_keys
    write_keys = {_collision_key(e.path) for e in approved}
    rejections.extend(
        _check_prefix_conflicts([e.path for e in approved], base_file_keys | write_keys)
    )
    rejections.extend(_check_prefix_conflicts(list(approved_deletions), base_file_keys))

    # Cache exclusion comes last. Every cache entry has therefore passed the
    # same path, mode, content, collision, prefix, and review controls as code.
    # A tracked cache path remains ordinary repository content. Deletions are
    # tracked by definition and are never excluded.
    scoped_approved = _remove_untracked_caches(approved, destination_keys)

    return FilterVerdict(
        _approved=tuple(scoped_approved),
        _deletions=tuple(approved_deletions),
        rejections=tuple(rejections),
        flags=tuple(flags),
    )


class _ArtifactLike(Protocol):
    """The three things an artifact must offer, checked rather than asserted.

    ``object`` plus ``type: ignore`` would have switched type checking off at
    precisely the seam whose job is to stop these three drifting apart — so the
    adapter states the shape and lets mypy verify all three accesses, including
    ``text_for``'s signature.

    Declared as read-only PROPERTIES rather than attributes. A bare
    ``entries: Sequence[...]`` in a Protocol means a *settable* variable, which
    a frozen dataclass can never satisfy — so this protocol excluded
    :class:`kew.publication_artifact.Artifact`, the only thing it was ever meant
    to describe. Nothing noticed while node 4 had no callers; the first one in
    ``src/`` failed to type-check immediately.
    """

    @property
    def entries(self) -> Sequence[ManifestEntry]: ...

    @property
    def deletions(self) -> Sequence[str]: ...

    def text_for(self, path: str, /) -> str: ...


def filter_artifact(
    artifact: _ArtifactLike,
    *,
    existing_modes: Mapping[str, str],
    destination_paths: Iterable[str],
    merged_base_digest: Callable[[str], str | None] | None = None,
) -> FilterVerdict:
    """Filter a whole :class:`kew.publication_artifact.Artifact`.

    The adapter exists because the seam is where things get dropped. Calling
    ``filter_manifest(artifact.entries)`` and forgetting ``artifact.deletions``
    type-checks, reads naturally, passes tests, and silently discards every
    removal — and pairing ``read_text`` with some other reader would let a
    ``.gitattributes`` be approved on bytes other than the ones later applied.
    Forwarding all three together is the only way to be sure they describe one
    artifact.
    """
    return filter_manifest(
        artifact.entries,
        deletions=artifact.deletions,
        existing_modes=existing_modes,
        destination_paths=destination_paths,
        read_text=artifact.text_for,
        merged_base_digest=merged_base_digest,
    )
