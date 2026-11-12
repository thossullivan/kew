"""Which frontend checks a PR's merge verify runs: all, or what it owns (#3213).

With ``[verify] frontend = "scoped"`` a PR verify that changes ``frontend/``
runs, of vitest and ``ui-check``, only what the change owns:

* **vitest:** ``vitest related <changed files> --run``, vitest's own
  module-graph selection of the test files that import a changed file, and
  every test file that reads files rather than importing them (``fs``,
  ``import.meta.glob``, ``?raw``, ``new URL(..., import.meta.url)``): the
  graph never reaches those, so they always run, as the tiered suite always
  runs ``-m policy``;
* **ui-check:** the modules whose ``ui-check-owns:`` lines
  (:mod:`kew.ui_check_modules`) own a changed file or a file that imports
  one, the modules that import a changed file themselves (a helper, another
  module), the modules that read files at the base, as the vitest files above
  do, and the modules the change itself adds or edits. A module reads files
  through the shared ui-check library only when it imports one of the
  library's reading exports (:data:`SHARED_LIB_READING_EXPORTS`) or imports
  the library other than by a named list. Ownership is read off
  the BASE commit's modules: a candidate's own ``ui-check-owns:`` lines never
  decide what counts as covered, nor do imports it adds to existing files.

What reads files is a list of known APIs (:data:`_READS_FILES`): a test or
module that reads files some other way is covered only by the nightly.

``svelte-check``, the frontend build, the demo build and the www gates run
whole either way, and the nightly always runs the full vitest and ui-check.
The default, ``"full"``, changes nothing.

The selection is computed by the operator's kew from git objects, as the
tiered suite's is (:mod:`kew.verify_suite`): the candidate's code is read as
text and never run to decide what runs. Whatever puts it in doubt runs the
full vitest and ui-check instead, and says why:

* anything that keeps the tiered suite whole (:func:`verify_suite.selection_doubt`):
  the nightly, ``--full``, a sharded verify, a red or unreadable main, a
  missing or stale nightly;
* a change to the app shell, ``lib/ui``, the design tokens, the fixtures
  loader, ``package.json`` or a lockfile, a dotfile at ``frontend/``'s root
  (``.npmrc``), the build config or the ui-check runner (:data:`WHOLE_RULES`);
* the ownership map cannot be read;
* any deletion under ``frontend/``, whose importers and readers the graph can
  no longer find;
* a changed file outside ``frontend/`` that frontend code imports
  (``kew/src/kew/models.json``): ``vitest related`` is handed only the
  frontend paths;
* a changed surface file that no module with ``ui-check-owns:`` lines owns or
  imports through an owned file that is no unit test, even while a module with
  none is left (#3409): a scoped run checks nothing known about it. A static
  asset or a fixture or demo data file is covered only by a module that names
  it, never by an import, even when its name reads as a unit test;
* a module with no ``ui-check-owns:`` line has an unknown surface, so every
  frontend change selects it;
* a selection of :data:`WHOLE_SHARE` of the modules or more, as a change to a
  file nearly every page imports (``lib/api.ts``, ``lib/factory/names.ts``)
  makes: the modules that own those pages all run.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from kew import main_health, verify_selection_report
from kew import verify_selection as vs
from kew.ui_check_modules import UI_CHECKS_DIR, glob_matches, owned_globs
from kew.verify_suite import SuiteRequest, selection_doubt

if TYPE_CHECKING:
    from kew.run_store import RunStore

#: ``[verify] frontend``: everything, or what the change owns when that is safe.
FRONTEND_FULL = "full"
FRONTEND_SCOPED = "scoped"

#: What a verify ran of vitest and ui-check. Main's health reads only a
#: ``full`` one (:mod:`kew.main_health`).
MODE_FULL = main_health.FRONTEND_FULL
MODE_SCOPED = main_health.FRONTEND_SCOPED

#: Bumped whenever the rules below change what a selection holds.
SELECTOR_VERSION = 4

#: A selection holding this share of the modules or more runs them all.
WHOLE_SHARE = 0.7

DEFAULT_REASON = '[verify] frontend = "full"'
NO_FRONTEND_REASON = (
    "the change touches nothing under frontend/: vitest runs whole, as before"
)

_FRONTEND = "frontend/"

#: Changed paths that run the full vitest and ui-check, by why. Repo-rooted
#: globs (:func:`kew.ui_check_modules.glob_matches`).
WHOLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "the app shell",
        (
            "frontend/src/app.html",
            "frontend/src/app.d.ts",
            "frontend/src/hooks.*",
            "frontend/src/routes/+layout*",
            "frontend/src/routes/(factory)/+layout*",
            "frontend/src/lib/shell/**",
        ),
    ),
    ("lib/ui", ("frontend/src/lib/ui.*", "frontend/src/lib/ui/**")),
    ("the design tokens", ("frontend/src/app.css",)),
    (
        "the fixtures loader",
        ("frontend/fixtures/plugin.ts", "frontend/fixtures/core.ts"),
    ),
    (
        "package.json or a lockfile",
        (
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/npm-shrinkwrap.json",
            "frontend/yarn.lock",
            "frontend/pnpm-lock.yaml",
        ),
    ),
    ("a frontend root dotfile", ("frontend/.*",)),
    (
        "the build config",
        (
            "frontend/vite.config.*",
            "frontend/svelte.config.*",
            "frontend/*.config.*",
            "frontend/tsconfig.json",
            "frontend/demo/vite.demo.config.*",
        ),
    ),
    (
        "the ui-check runner",
        (
            "frontend/scripts/ui-check.mjs",
            "frontend/scripts/ui-check-lib.mjs",
            "frontend/scripts/ui-check.base.json",
            "frontend/scripts/static-server.mjs",
        ),
    ),
)

#: What some surface renders or serves: a change here no module covers is a
#: change a scoped ui-check would not look at.
_SURFACE_ROOTS = (
    "frontend/src/",
    "frontend/fixtures/",
    "frontend/static/",
    "frontend/demo/",
)
#: Served as they are, or fetched over HTTP: no import reaches them.
_STATIC = "frontend/static/"
_DATA_ROOTS = ("frontend/fixtures/", "frontend/demo/")
#: The data files a fixture or demo page fetches, matched lowercased.
_DATA_SUFFIXES = (".json", ".jsonl", ".json5")
#: A unit test renders nothing (``*.test.ts``, ``*.client.test.ts``).
_TEST = re.compile(r"\.(?:test|spec)\.[^/]+$")
#: A vitest file, as ``vite.config.ts`` includes them.
_VITEST_FILE = re.compile(r"^frontend/(?:src|scripts)/.*\.test\.ts$")
#: Prose, never imported or rendered.
_PROSE = re.compile(r"\.md$")

#: Files the import graph reads, and the most of one it reads.
_SOURCE = re.compile(r"\.(?:ts|js|mjs|cjs|svelte)$")
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_REGULAR_MODES = frozenset({"100644", "100755"})

#: ``from 'x'``, ``import 'x'``, ``import('x')``: over-reading a comment
#: only selects more.
_SPECIFIER = re.compile(r"""(?:\bfrom|\bimport)\s*\(?\s*['"]([^'"\n]+)['"]""")
#: ``new URL('x', import.meta.url)``: a file a module names without importing.
_URL_SPECIFIER = re.compile(
    r"""\bnew\s+URL\s*\(\s*['"]([^'"\n]+)['"]\s*,\s*import\.meta\.url"""
)
#: A source that reads files rather than importing them: ``fs`` (or a child
#: process), ``import.meta.glob``, a ``?raw``/``?url``/``?inline`` import, or a
#: path resolved against ``import.meta.url``. ``vitest related`` follows
#: imports only, so a test that reads its subject never relates to it.
#: Over-reading a comment only runs more.
#: It is a list of known APIs, quoted any of the three ways: a source that
#: reads files some other way is covered only by the nightly.
_READS_FILES = re.compile(
    r"""['"`](?:(?:node:)?(?:fs|fs/promises|child_process)"""
    r"""|fs-extra|graceful-fs|glob|fast-glob|tinyglobby|globby)['"`]"""
    r"""|\bimport\.meta\.(?:glob|url)\b"""
    r"""|\?(?:raw|url|inline)\b"""
)
_LIB_ALIAS = "$lib"
#: The ui-check library nearly every module imports for its constants. It
#: reads files, but a change to it runs everything (:data:`WHOLE_RULES`).
_SHARED_LIB = "frontend/scripts/ui-check-lib.mjs"
#: The library's exports that read files or name a path to one: a source that
#: imports one, or imports the library any way but a named list, reads files
#: through it. A ``-m policy`` test reads the library and holds this complete.
SHARED_LIB_READING_EXPORTS = frozenset(
    {
        "FRONTEND",
        "CHECKS_DIR",
        "BUILD_DIR",
        "DEMO_BUILD_DIR",
        "docsPassage",
        "servedProvenance",
        "buildSentinels",
        "buildLeaks",
        "checkModules",
        "owningModules",
        "selectModules",
        "scopedModules",
        "runChecks",
        "checksDirFrom",
    }
)
#: ``import { a, b as c } from 'x'``: the one way to import the library that
#: says what is used.
_NAMED_IMPORT = re.compile(r"""\bimport\s*\{([^}]*)\}\s*from\s*['"]([^'"\n]+)['"]""")
_WORD = re.compile(r"[\w$]+")
_EXTENSIONS = ("", ".ts", ".js", ".svelte", ".svelte.ts", ".svelte.js", ".mjs")
_INDEXES = ("/index.ts", "/index.js")

#: A module name and a changed path the job may pass as plain words.
_MODULE_NAME = re.compile(r"\A[\w.-]+\Z")
_PLAIN_PATH = re.compile(r"\A[\w./()\[\]+@ -]+\Z")


class OwnershipUnreadable(vs.SelectionError):
    """The ui-check ownership map cannot be read: run every module."""


@dataclass(frozen=True)
class Change:
    """One changed path, as ``git diff --name-status`` reports it."""

    status: str
    path: str

    @property
    def deleted(self) -> bool:
        return self.status.startswith("D")


@dataclass(frozen=True)
class FrontendTree:
    """What the selection reads of one commit's ``frontend/``."""

    #: Every regular file under ``frontend/``, repo-rooted.
    files: frozenset[str]
    #: The text of each source file the import graph reads.
    sources: Mapping[str, str]
    #: Each ui-check module by name (no ``.mjs``), with its text.
    modules: Mapping[str, str]

    @cached_property
    def specifiers(self) -> Mapping[str, tuple[str, ...]]:
        """Each source, with what it imports as written (:data:`_SPECIFIER`):
        read once, though a selection walks the graph several times."""
        return {
            path: tuple(_SPECIFIER.findall(text)) for path, text in self.sources.items()
        }

    @cached_property
    def readers(self) -> tuple[str, ...]:
        """The sources that read files themselves (:data:`_READS_FILES`)."""
        return tuple(
            path for path, text in self.sources.items() if _READS_FILES.search(text)
        )


@dataclass(frozen=True)
class FrontendSelection:
    """What a verify runs of vitest and ui-check, and why."""

    mode: str = MODE_FULL
    reason: str = DEFAULT_REASON
    #: The ui-check modules a scoped verify runs (no ``.mjs``).
    modules: tuple[str, ...] = ()
    #: The changed files, relative to ``frontend/``, given to ``vitest related``.
    related: tuple[str, ...] = ()
    #: The vitest files, relative to ``frontend/``, that read files rather than
    #: import them (:data:`_READS_FILES`), at the base commit: they run in
    #: every scoped verify, handed to ``vitest related`` beside :attr:`related`.
    always: tuple[str, ...] = ()
    digest: str | None = None
    #: Whether ``[verify] frontend`` asked for a scoped run at all.
    scoped_setting: bool = False
    #: The import graph's own reading of the vitest files ``related`` runs;
    #: the sizing report's estimate, never what the job runs.
    tests: tuple[str, ...] = ()
    total_modules: int = 0
    total_tests: int = 0

    @property
    def scoped(self) -> bool:
        return self.mode == MODE_SCOPED

    def payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "modules": list(self.modules),
            "related": list(self.related),
            "always": list(self.always),
            "digest": self.digest,
            "selector_version": SELECTOR_VERSION if self.scoped_setting else None,
        }

    def describe(self) -> str:
        """One line for an operator."""
        if self.scoped:
            return (
                f"frontend: scoped, ui-check {len(self.modules)} of "
                f"{self.total_modules} modules, vitest related on "
                f"{len(self.related)} changed files and {len(self.always)} "
                f"file-reading tests ({self.reason}); "
                "svelte-check and the builds run whole"
            )
        return f"frontend: full ({self.reason})"


FULL_FRONTEND = FrontendSelection()


def _full(
    reason: str, *, total_modules: int = 0, total_tests: int = 0
) -> FrontendSelection:
    return FrontendSelection(
        mode=MODE_FULL,
        reason=reason,
        scoped_setting=True,
        total_modules=total_modules,
        total_tests=total_tests,
    )


def choose_frontend(
    setting: str,
    request: SuiteRequest,
    *,
    store: RunStore | None,
    now: datetime | None = None,
) -> FrontendSelection:
    """What this verify runs of vitest and ui-check: :data:`FULL_FRONTEND`
    unless *setting* is scoped and nothing puts the selection in doubt."""
    if setting != FRONTEND_SCOPED:
        return FULL_FRONTEND
    doubt = selection_doubt(request, store=store, now=now)
    if doubt is not None:
        return _full(doubt)
    root = request.repo_root
    try:
        changes = changed(root, request.base_sha, request.ref_sha)
        selection = select(
            changes,
            read_tree(root, request.ref_sha),
            read_tree(root, request.base_sha),
        )
    except OwnershipUnreadable as exc:
        return _full(f"the ui-check ownership map cannot be read: {exc}")
    # Fail closed: whatever stops the selection runs the full frontend checks.
    except Exception as exc:  # noqa: BLE001
        return _full(f"the frontend selection could not be computed: {exc}")
    return selection


# --- git objects ------------------------------------------------------------


def changed(root: Path, base: str, ref: str) -> tuple[Change, ...]:
    """Every path *ref* changes against *base*, renames as delete plus add."""
    out = vs._git(root, "diff", "--name-status", "-z", "--no-renames", base, ref)
    words = [word for word in out.split("\0") if word]
    return tuple(
        Change(status, path)
        for status, path in zip(words[::2], words[1::2], strict=True)
    )


def read_tree(root: Path, rev: str) -> FrontendTree:
    """*rev*'s ``frontend/``, read from git objects: never a checkout."""
    out = vs._git(root, "ls-tree", "-r", "-z", "--full-tree", rev, "--", "frontend")
    files, wanted, module_oids = _sort_entries(out)
    if not module_oids:
        raise OwnershipUnreadable(f"{rev[:12]} has no module in {UI_CHECKS_DIR}/")
    blobs = vs.cat_blobs(root, sorted({*wanted.values(), *module_oids.values()}))
    return FrontendTree(
        files=frozenset(files),
        sources={path: _text(path, blobs[oid]) for path, oid in wanted.items()},
        modules={
            name: _module_text(name, blobs[oid]) for name, oid in module_oids.items()
        },
    )


def _ls_tree_entries(out: str) -> Iterator[tuple[str, str, str, str]]:
    """``(mode, kind, oid, path)`` for each entry ``git ls-tree -z`` printed."""
    for entry in out.split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            mode, kind, oid = meta.split()
            yield mode, kind, oid, path


def _sort_entries(
    out: str,
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """Every regular file, the sources the graph reads, and the modules by name."""
    files: set[str] = set()
    wanted: dict[str, str] = {}
    module_oids: dict[str, str] = {}
    for mode, kind, oid, path in _ls_tree_entries(out):
        if posixpath.dirname(path) == UI_CHECKS_DIR and path.endswith(".mjs"):
            if mode not in _REGULAR_MODES:
                raise OwnershipUnreadable(f"{path} is no regular file (mode {mode})")
            module_oids[posixpath.basename(path).removesuffix(".mjs")] = oid
        if kind == "blob" and mode in _REGULAR_MODES:
            files.add(path)
            if _SOURCE.search(path):
                wanted[path] = oid
    return files, wanted, module_oids


def _text(path: str, blob: bytes) -> str:
    if len(blob) > _MAX_SOURCE_BYTES:
        # An unread file's imports would be missed: refuse rather than under-select.
        raise vs.SelectionError(f"{path} is over {_MAX_SOURCE_BYTES} bytes")
    return blob.decode("utf-8", errors="replace")


def _module_text(name: str, blob: bytes) -> str:
    try:
        return _text(f"{UI_CHECKS_DIR}/{name}.mjs", blob)
    except vs.SelectionError as exc:
        raise OwnershipUnreadable(str(exc)) from None


# --- the import graph ---------------------------------------------------------


def _resolve(importer: str, spec: str, files: frozenset[str]) -> str | None:
    """The file *importer*'s ``import spec`` names, or ``None`` (a package)."""
    spec = spec.split("?", 1)[0]
    if spec == _LIB_ALIAS or spec.startswith(f"{_LIB_ALIAS}/"):
        base = "frontend/src/lib" + spec.removeprefix(_LIB_ALIAS)
    elif spec.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    else:
        return None
    stems = [base]
    if base.endswith(".js"):
        stems.append(base.removesuffix(".js"))
    for stem in stems:
        for suffix in (*_EXTENSIONS, *_INDEXES):
            if stem + suffix in files:
                return stem + suffix
    return None


def dependents(tree: FrontendTree) -> dict[str, set[str]]:
    """Each file, with the files that import it directly."""
    graph: dict[str, set[str]] = {}
    for importer, specs in tree.specifiers.items():
        for spec in specs:
            target = _resolve(importer, spec, tree.files)
            if target is not None and target != importer:
                graph.setdefault(target, set()).add(importer)
    return graph


def _merged(*graphs: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """Every edge of every graph: an import the change drops still selects."""
    merged: dict[str, set[str]] = {}
    for graph in graphs:
        for target, importers in graph.items():
            merged.setdefault(target, set()).update(importers)
    return merged


def _importers(start: Iterable[str], graph: Mapping[str, set[str]]) -> set[str]:
    """*start* and every file that imports one of them, however indirectly."""
    seen = set(start)
    queue = deque(seen)
    while queue:
        for importer in graph.get(queue.popleft(), ()):
            if importer not in seen:
                seen.add(importer)
                queue.append(importer)
    return seen


def _reading(tree: FrontendTree, *, lib_exemption: bool = False) -> set[str]:
    """The files of *tree* that read files rather than import them
    (:data:`_READS_FILES`), and every file that imports one, however
    indirectly: the import graph never relates them to what they read.
    With *lib_exemption*, a file that imports from the shared library
    (:data:`_SHARED_LIB`) only what reads nothing is no reader for it."""
    graph = dependents(tree)
    if lib_exemption and _SHARED_LIB in graph:
        graph[_SHARED_LIB] = {
            importer
            for importer in graph[_SHARED_LIB]
            if _reads_through_lib(importer, tree)
        }
    return _importers(tree.readers, graph)


def _reads_through_lib(importer: str, tree: FrontendTree) -> bool:
    """Whether *importer* uses what the shared library reads: it imports a
    name of :data:`SHARED_LIB_READING_EXPORTS`, or imports the library any way
    but a named list (``* as``, a default, ``import()``, a re-export)."""
    text = tree.sources[importer]
    named: set[int] = set()
    for match in _NAMED_IMPORT.finditer(text):
        if _resolve(importer, match.group(2), tree.files) != _SHARED_LIB:
            continue
        if SHARED_LIB_READING_EXPORTS.intersection(_WORD.findall(match.group(1))):
            return True
        named.add(match.end())
    return any(
        match.end() not in named
        for match in _SPECIFIER.finditer(text)
        if _resolve(importer, match.group(1), tree.files) == _SHARED_LIB
    )


def file_reading_tests(tree: FrontendTree) -> list[str]:
    """The vitest files of *tree* that read files (:func:`_reading`):
    ``vitest related`` never relates them to what they read."""
    return sorted(path for path in _reading(tree) if _VITEST_FILE.search(path))


def _module_path(name: str) -> str:
    """The repo-rooted file of the ui-check module *name*."""
    return f"{UI_CHECKS_DIR}/{name}.mjs"


def file_reading_modules(tree: FrontendTree) -> list[str]:
    """The ui-check modules of *tree* that read files themselves or through a
    helper they import (:func:`_reading`), by name: no ownership line or
    import says which changed file they check. Importing from the shared
    library (:data:`_SHARED_LIB`) only names that read nothing does not make
    a reader (:func:`_reads_through_lib`)."""
    reading = _reading(tree, lib_exemption=True)
    return sorted(name for name in tree.modules if _module_path(name) in reading)


def _outside_targets(tree: FrontendTree) -> set[str]:
    """The repo-rooted paths outside ``frontend/`` a frontend source imports,
    or names against ``import.meta.url``, as written: no extension resolved.

    A vitest file that names one against ``import.meta.url`` reads it, so it
    runs in every scoped verify (:func:`file_reading_tests`) and needs no
    fallback; what it imports still counts.
    """
    targets: set[str] = set()
    for importer, text in tree.sources.items():
        if not importer.startswith(_FRONTEND):
            continue
        named = [] if _VITEST_FILE.search(importer) else _URL_SPECIFIER.findall(text)
        for spec in (*tree.specifiers[importer], *named):
            spec = spec.split("?", 1)[0]
            if not spec.startswith("."):
                continue
            here = posixpath.dirname(importer)
            target = posixpath.normpath(posixpath.join(here, spec))
            if _outside(target):
                targets.add(target)
    return targets


def _outside(target: str) -> bool:
    """Whether a normalised repo-rooted *target* is in the repository but
    outside ``frontend/``."""
    return not (
        target.startswith((_FRONTEND, "../"))
        or target in (".", "..", _FRONTEND.rstrip("/"))
    )


def _names(target: str, path: str) -> bool:
    """Whether an import of *target* may load *path*: the file itself, it with
    any extension the graph resolves, or anything under it as a directory."""
    if path.startswith(target + "/"):
        return True
    stems = (target, target.removesuffix(".js"))
    return any(path == stem + suffix for stem in stems for suffix in _EXTENSIONS)


def _outside_change(changes: Iterable[Change], targets: set[str]) -> str | None:
    """The first changed path outside ``frontend/`` that frontend code imports."""
    for change in changes:
        if change.path.startswith(_FRONTEND):
            continue
        if any(_names(target, change.path) for target in targets):
            return change.path
    return None


# --- the selection --------------------------------------------------------------


@dataclass(frozen=True)
class _Inputs:
    #: The changes under ``frontend/``.
    changes: tuple[Change, ...]
    #: Every change, under ``frontend/`` or not.
    every_change: tuple[Change, ...]
    head: FrontendTree
    base: FrontendTree
    #: The ownership map: the base commit's modules, never the candidate's.
    modules: Mapping[str, str]
    owned: dict[str, frozenset[str]] = field(default_factory=dict)
    #: The modules the change adds or edits: they run, from the head.
    edited: frozenset[str] = frozenset()
    #: The map's modules that read files at the base: they always run.
    reading_modules: frozenset[str] = frozenset()

    @property
    def related(self) -> tuple[str, ...]:
        """The changed files ``vitest related`` is handed, relative to
        ``frontend/``: every one the head still has."""
        return tuple(
            c.path.removeprefix(_FRONTEND) for c in self.changes if not c.deleted
        )

    @property
    def total(self) -> int:
        """Every module a full run would make: the map's and the change's."""
        return len(set(self.modules) | self.edited)


def select(
    changes: Sequence[Change],
    head: FrontendTree,
    base: FrontendTree,
    *,
    modules: Mapping[str, str] | None = None,
) -> FrontendSelection:
    """What a change from *base* to *head* runs of vitest and ui-check.

    What decides coverage is read off *base*, never the candidate: the
    ownership map, the file-reading tests and the files outside ``frontend/``
    that frontend code imports. *modules* replaces the base's map only when
    the sizing report replays an old change against today's
    (:func:`sizing_report`). The import graph holds both commits' edges.
    """
    inputs = _inputs(changes, head, base, base.modules if modules is None else modules)
    tests = sorted(path for path in head.files if _VITEST_FILE.search(path))
    graph = _merged(dependents(head), dependents(base))
    picked = _picked_modules(inputs, graph)
    related = inputs.related
    always = _always(inputs)
    why = _fallback(inputs, picked, (*related, *always))
    if why is not None:
        return _full(why, total_modules=inputs.total, total_tests=len(tests))
    selection = FrontendSelection(
        mode=MODE_SCOPED,
        reason=f"{len(picked)} of {inputs.total} ui-check modules own the change",
        modules=tuple(picked),
        related=related,
        always=always,
        scoped_setting=True,
        tests=_estimate(inputs, graph, always, tests),
        total_modules=inputs.total,
        total_tests=len(tests),
    )
    return _with_digest(selection)


def _inputs(
    changes: Sequence[Change],
    head: FrontendTree,
    base: FrontendTree,
    ownership: Mapping[str, str],
) -> _Inputs:
    frontend = tuple(c for c in changes if c.path.startswith(_FRONTEND))
    return _Inputs(
        changes=frontend,
        every_change=tuple(changes),
        head=head,
        base=base,
        modules=ownership,
        owned=_owned_files(ownership, head.files),
        edited=_edited(frontend, head),
        reading_modules=frozenset(
            name for name in file_reading_modules(base) if name in ownership
        ),
    )


def _always(inputs: _Inputs) -> tuple[str, ...]:
    """The base's file-reading tests the head still has, relative to
    ``frontend/``: a scoped verify runs them whatever changed."""
    return tuple(
        path.removeprefix(_FRONTEND)
        for path in file_reading_tests(inputs.base)
        if path in inputs.head.files
    )


def _estimate(
    inputs: _Inputs,
    graph: Mapping[str, set[str]],
    always: Sequence[str],
    tests: Sequence[str],
) -> tuple[str, ...]:
    """The import graph's reading of the vitest files a scoped run runs:
    those that reach a changed file, and every file-reading test."""
    reached = _importers((c.path for c in inputs.changes), graph)
    reached.update(_FRONTEND + path for path in always)
    return tuple(path for path in tests if path in reached)


def _edited(changes: Iterable[Change], head: FrontendTree) -> frozenset[str]:
    """The ui-check modules the change adds or edits, by name."""
    return frozenset(
        name
        for c in changes
        if not c.deleted and posixpath.dirname(c.path) == UI_CHECKS_DIR
        if (name := posixpath.basename(c.path).removesuffix(".mjs")) in head.modules
    )


def _fallback(
    inputs: _Inputs,
    picked: Sequence[str],
    words: Sequence[str],
) -> str | None:
    """Why this change runs the full vitest and ui-check, or ``None``."""
    why = _change_reason(inputs)
    if why is not None:
        return why
    uncovered = _uncovered(inputs)
    if uncovered is not None:
        return uncovered
    total = inputs.total
    if total and len(picked) / total >= WHOLE_SHARE:
        return (
            f"the selection is {len(picked)} of {total} modules, "
            f"{WHOLE_SHARE:.0%} or more"
        )
    odd = _odd_word(picked, words)
    if odd is not None:
        return f"a selected name is no plain word: {odd!r}"
    return None


def _change_reason(inputs: _Inputs) -> str | None:
    """Why the changed paths alone run everything, or ``None``."""
    if not inputs.changes:
        return NO_FRONTEND_REASON
    why = _whole_reason(inputs.changes)
    if why is not None:
        return why
    targets = _outside_targets(inputs.base) | _outside_targets(inputs.head)
    outside = _outside_change(inputs.every_change, targets)
    if outside is not None:
        return (
            "frontend code imports or reads a changed file outside frontend/: "
            f"{outside}"
        )
    return None


def _odd_word(picked: Sequence[str], related: Sequence[str]) -> str | None:
    """A module name or path the job could not pass as one word."""
    odd = next((name for name in picked if not _MODULE_NAME.match(name)), None)
    return odd or next((path for path in related if not _plain_path(path)), None)


def _plain_path(path: str) -> bool:
    """Whether the job may pass *path* to ``vitest related`` as one word."""
    return _PLAIN_PATH.match(path) is not None and not path.startswith("-")


def _whole_reason(changes: Iterable[Change]) -> str | None:
    """The first rule that runs everything, with the path that fired it."""
    for change in changes:
        for why, globs in WHOLE_RULES:
            if any(glob_matches(glob, change.path) for glob in globs):
                return f"{why}: {change.path}"
        if change.deleted:
            return (
                "a deletion under frontend/, whose importers and readers cannot "
                f"be traced: {change.path}"
            )
        if not change.status.startswith(("A", "M")):
            return f"a change git reports as {change.status}: {change.path}"
    return None


def _renders(path: str) -> bool:
    """Whether *path* is part of what some surface renders or serves."""
    return (
        path.startswith(_SURFACE_ROOTS)
        and not _TEST.search(path)
        and not _PROSE.search(path)
    )


def _a_test(path: str) -> bool:
    """Whether *path* is a unit test: it renders nothing, so importing a file
    covers nothing (:func:`_uncovered`)."""
    return bool(_TEST.search(path) or _VITEST_FILE.search(path))


def _owned_files(
    modules: Mapping[str, str], files: Iterable[str]
) -> dict[str, frozenset[str]]:
    """Each module, with the files of the tree its ownership lines name."""
    rels = [
        path.removeprefix(_FRONTEND) for path in files if path.startswith(_FRONTEND)
    ]
    owned: dict[str, frozenset[str]] = {}
    for name, source in modules.items():
        globs = owned_globs(source)
        owned[name] = frozenset(
            _FRONTEND + rel for rel in rels if any(glob_matches(g, rel) for g in globs)
        )
    return owned


def _unmapped(inputs: _Inputs) -> list[str]:
    """The map's modules that carry no ``ui-check-owns:`` line: their surface
    is unknown, so any frontend change may be one they check, and they run."""
    return [name for name, source in inputs.modules.items() if not owned_globs(source)]


def _uncovered(inputs: _Inputs) -> str | None:
    """Why the first uncovered changed surface file runs everything: no module
    of the map owns it or a file that imports it and is no unit test. A test
    that imports a fixture renders nothing, while the modules that render the
    fixture load a page and get it over HTTP, never by import.

    Only the modules with ownership lines count, whether or not the map still
    has a module with none (#3409): an unmapped module runs for every change,
    yet its surface is unknown, so it is no proof that the change is checked.
    A static asset or a fixture or demo data file is covered only by a module
    that names it (:func:`_named_only`), whatever its name: the pages that
    serve it fetch it over HTTP, so no import reaches it. A module the change
    adds covers nothing here: its ownership lines are the candidate's own. Nor
    does an import the change adds to a file the base has: coverage is read
    off the base's graph, plus the imports of the files the change adds
    (:func:`_coverage_graph`).
    """
    graph = _coverage_graph(inputs)
    named = frozenset().union(*inputs.owned.values())
    every_owned = frozenset(path for path in named if not _a_test(path))
    for change in inputs.changes:
        path = change.path
        if change.deleted:
            continue
        if _named_only(path):
            if path not in named:
                return f"no ui-check module with ownership lines names {path}"
        elif _renders(path) and not _importers([path], graph) & every_owned:
            return (
                f"no ui-check module with ownership lines owns {path} "
                "or a file that imports it"
            )
    return None


def _named_only(path: str) -> bool:
    """Whether only an ownership line naming *path* covers it: a static asset,
    or a fixture or demo data file (JSON, JSON Lines, JSON5, any case)."""
    return path.startswith(_STATIC) or (
        path.startswith(_DATA_ROOTS) and path.lower().endswith(_DATA_SUFFIXES)
    )


def _coverage_graph(inputs: _Inputs) -> dict[str, set[str]]:
    """The base's import graph, plus the edges out of the files the change
    adds: never an edge the change adds to a file the base already has."""
    added = {c.path for c in inputs.changes if c.status.startswith("A")}
    from_added = {
        target: importers & added
        for target, importers in dependents(inputs.head).items()
        if importers & added
    }
    return _merged(dependents(inputs.base), from_added)


def _picked_modules(inputs: _Inputs, graph: Mapping[str, set[str]]) -> list[str]:
    """The modules the change adds or edits, those whose owned files or whose
    own file reach a changed file (a module importing a changed helper or
    module), those that read files (:func:`file_reading_modules`), and those
    whose surface is unknown (:func:`_unmapped`)."""
    reached = _importers((c.path for c in inputs.changes), graph)
    always = inputs.reading_modules | set(_unmapped(inputs))
    owners = {
        name
        for name, owned in inputs.owned.items()
        if name in always or owned & reached or _module_path(name) in reached
    }
    return sorted(owners | inputs.edited)


def _with_digest(selection: FrontendSelection) -> FrontendSelection:
    body = json.dumps(
        {
            "selector_version": SELECTOR_VERSION,
            "modules": list(selection.modules),
            "related": list(selection.related),
            "always": list(selection.always),
        },
        sort_keys=True,
    )
    return replace(selection, digest=hashlib.sha256(body.encode()).hexdigest())


# --- the sizing report ------------------------------------------------------------


def main_tip(root: Path, branch: str = "main") -> str:
    """The commit ``origin/<branch>``, or else ``<branch>``, names."""
    for name in (f"origin/{branch}", branch):
        try:
            return vs._git(root, "rev-parse", "--verify", f"{name}^{{commit}}").strip()
        except vs.SelectionError:
            continue
    raise vs.SelectionError(f"neither origin/{branch} nor {branch} resolves")


def frontend_commits(root: Path, last: int, tip: str) -> list[str]:
    """The last *last* first-parent commits up to *tip* that change ``frontend/``."""
    out = vs._git(
        root, "rev-list", "--first-parent", f"--max-count={last}", tip, "--", "frontend"
    )
    return [sha for sha in out.split() if sha]


def _replay(
    root: Path, sha: str, modules: Mapping[str, str]
) -> FrontendSelection | None:
    """The selection *sha*'s change would make today; ``None`` for a root commit."""
    parents = vs._git(root, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    if not parents:
        return None
    head, base = read_tree(root, sha), read_tree(root, parents[0])
    return select(changed(root, parents[0], sha), head, base, modules=modules)


def _shares(selection: FrontendSelection) -> tuple[float, float]:
    """The selected share of ui-check modules and of vitest files."""
    if not selection.scoped:
        return 1.0, 1.0
    modules = len(selection.modules) / max(selection.total_modules, 1)
    tests = len(selection.tests) / max(selection.total_tests, 1)
    return modules, tests


def sizing_report(root: Path, commits: Sequence[str], *, map_rev: str) -> str:
    """``kew verify --frontend-report``: each frontend commit replayed against
    today's ownership map (the one at *map_rev*), and the median and p75
    selected share of ui-check modules and of vitest files.

    The vitest share is the import graph's estimate; the job itself asks
    ``vitest related``. A full fallback counts as everything.
    """
    modules = read_tree(root, map_rev).modules
    lines: list[str] = []
    ui: list[float] = []
    vitest: list[float] = []
    fallbacks = 0
    for sha in commits:
        try:
            selection = _replay(root, sha, modules)
        except vs.SelectionError as exc:
            selection = _full(f"the frontend selection could not be computed: {exc}")
        if selection is None:
            continue
        ui_share, test_share = _shares(selection)
        ui.append(ui_share)
        vitest.append(test_share)
        fallbacks += 0 if selection.scoped else 1
        lines.append(f"{sha[:12]}  {_replay_line(selection)}")
    head = (
        f"frontend sizing: {len(ui)} frontend commits on main, replayed against "
        f"the ownership map at {map_rev[:12]}"
    )
    if not ui:
        return f"{head}\nno frontend commit to replay\n"
    summary = [
        _spread("ui-check modules", ui),
        _spread("vitest files (import-graph estimate)", vitest),
        f"full fallbacks: {fallbacks} of {len(ui)}",
    ]
    return "\n".join([head, *lines, *summary]) + "\n"


def _replay_line(selection: FrontendSelection) -> str:
    if not selection.scoped:
        return f"full: {selection.reason}"
    return (
        f"scoped: ui-check {len(selection.modules)}/{selection.total_modules} "
        f"({', '.join(selection.modules) or 'none'}), vitest "
        f"{len(selection.tests)}/{selection.total_tests}"
    )


def _spread(label: str, values: Sequence[float]) -> str:
    quantile = verify_selection_report._quantile
    return (
        f"{label}: median {quantile(values, 0.5):.1%}, p75 {quantile(values, 0.75):.1%}"
    )
