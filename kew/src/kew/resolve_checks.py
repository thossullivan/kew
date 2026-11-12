"""The node's checks, run on a resolved tree before kew pushes it (#3200).

A base integration that git applies cleanly, or a conflict an agent resolved,
can still leave a tree that does not build. On 2026-10-25 the merge-main step
pushed a resolution onto agent/issue-3148 that reintroduced ``atRoute`` in
``controller.ts``, a symbol an operator fix had removed, and the branch stopped
compiling. Nothing between the resolution and the push ran the tree, and a
Python-only gate would have passed it: the break was TypeScript.

So every resolution kew publishes runs the operator's configured checks first
(``[resolve] python_checks`` and ``[resolve] frontend_checks``), each when the
integration changed files of its kind. A check that fails, times out or cannot
run raises :class:`ResolutionChecksFailed`, and the caller pushes nothing.

This is a cheap guard before a push, not the merge gate: the merge verify runs
the full suite before anything merges. A check is meant to be a whole-tree
static gate (lint, types, ``svelte-check``), because a semantic conflict breaks
files the integration never touched: main removes ``helper()`` from
``util.py`` while the branch adds a caller of it, and only ``util.py`` changed.
``{paths}`` is optional, for an operator who wants a narrower check.

The tree is agent-written, so the commands run under the containment-first
admission the merge queue's verify resolves, never as a bare host child unless
the operator's own ``dangerously_skip_containment`` produced that posture.

kew's own check image runs ``kew-check-frontend``, whose copy of the checkout's
``frontend/`` points ``node_modules`` at the image's, on the container's
read-only root. Vite bundles ``vite.config.ts`` into the nearest
``node_modules/.vite-temp``, which it could not create there, so on 2026-11-06
every frontend check failed with ENOENT on any tree, the operator's correct
merge included (#3495); the check container now mounts a writable tmpfs at that
one path (:data:`kew.sandbox.CHECK_IMAGE_SCRATCH_PATHS`). The hold names the
check's error lines, and the run's resolution evidence keeps its whole output.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from kew.credential_surface import redact_credentials
from kew.publication import sanitize_publication_evidence
from kew.subprocess_utils import run
from kew.worktree import (
    _checkout_read_command,
    checkout_content_read_env,
    checkout_no_fetch_env,
)

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.loops.verification import ConditionResult

_logger = logging.getLogger(__name__)

#: Suffixes the frontend checks (``svelte-check``, vitest) cover.
FRONTEND_SUFFIXES = (".ts", ".tsx", ".svelte", ".js", ".jsx", ".mjs", ".cjs")
PYTHON_SUFFIXES = (".py", ".pyi")

#: Longest a resolution check may run before it counts as not passed.
RESOLUTION_CHECK_TIMEOUT_SECONDS = 1800

#: Longest failing line a hold reason quotes.
_FAILING_LINE_CHARS = 200

#: Most error lines a hold reason quotes before it counts the rest.
_MAX_ERROR_LINES = 5

#: Longest text one field of the resolution evidence keeps.
_MAX_EVIDENCE_TEXT = 64 * 1024

#: The resolution evidence beside a run's log (#3495).
RESOLUTION_EVIDENCE_SUFFIX = ".resolution.json"

_EXIT_PREFIX = re.compile(r"^command exited -?\d+: ")
_STREAM_LABEL = re.compile(r"^(?:stdout|stderr): ")
_FAILURE_WORD = re.compile(r"error|fail", re.IGNORECASE)
#: An error line: svelte-check's ``Error:`` block, tsc's and mypy's
#: ``error:``/``error TS2322:``, vitest's ``FAIL`` and ``×`` lines and the
#: assertion it raised. svelte-check's ``Warn:`` lines and its summary are not.
_ERROR_LINE = re.compile(
    r"^(?:Error\b|FAIL\b|[×✗] )|\berror(?: TS\d+)?:|\b[A-Z]\w*Error:"
)
#: The ``path:line:col`` line svelte-check prints above each diagnostic.
_LOCATION_LINE = re.compile(r":\d+:\d+$")
#: Where ``kew-check-frontend`` copies the checkout inside the check image.
_CHECK_IMAGE_TREE = "/tmp/kew-check/tree/"  # nosec B108 - a path in the image


class ResolutionChecksFailed(RuntimeError):
    """The resolved tree did not pass its checks: nothing may be pushed.

    :attr:`summary` is the one line a hold reason carries: the config key of
    the check that did not pass and its error lines. The message adds the
    check's evidence; :attr:`output` is the check's whole sanitized output,
    which the run's resolution evidence keeps.
    """

    def __init__(
        self, summary: str, evidence: str = "", *, command: str = "", output: str = ""
    ) -> None:
        super().__init__(f"{summary}\n{evidence}" if evidence else summary)
        self.summary = summary
        self.command = command
        self.output = output


def _changed(worktree: Path, since: str, diff_filter: str) -> list[str]:
    # The agent wrote this checkout's config: the diff hashes working-tree
    # files, so it runs with every program that config names switched off.
    # A submodule's dirt is read by a child git under the submodule's own
    # config, which those switches never reach, so only its recorded commit
    # is compared (``dirty``): uncommitted submodule content is not published.
    env = checkout_content_read_env(worktree, base=os.environ)
    if env is None:
        raise ResolutionChecksFailed(
            "the resolved checkout's git configuration could not be read"
        )
    changed = run(
        _checkout_read_command(
            worktree,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=dirty",
            "--name-only",
            f"--diff-filter={diff_filter}",
            since,
        ),
        check=False,
        env=env,
    )
    if changed.returncode != 0:
        raise ResolutionChecksFailed(
            f"the files the resolution changed since {since[:12]} could not be read"
        )
    return changed.stdout.splitlines()


def touched_paths(worktree: Path, *, since: str) -> list[str]:
    """Files the resolution changed relative to the PR head *since*.

    Committed and uncommitted changes alike (``git diff <since>`` compares the
    working tree), plus untracked files the agent left: the host commits those
    too before it publishes. A file the resolution deleted is left out: it no
    longer exists to check, and a check handed its path fails on it.
    """
    lines = _changed(worktree, since, "d")
    untracked = run(
        _checkout_read_command(worktree, "ls-files", "--others", "--exclude-standard"),
        check=False,
        env=checkout_no_fetch_env(os.environ),
    )
    if untracked.returncode == 0:
        lines += untracked.stdout.splitlines()
    return sorted({line.strip() for line in lines if line.strip()})


def deleted_paths(worktree: Path, *, since: str) -> list[str]:
    """Files the resolution deleted relative to the PR head *since*.

    A deletion is a change of its kind too: main deleting ``pkg/util.py``
    while the branch adds a caller of it breaks the tree with no surviving
    Python file changed, so a whole-tree Python check still has to run.
    """
    return sorted({p.strip() for p in _changed(worktree, since, "D") if p.strip()})


def resolution_checks(
    config: DispatchConfig, paths: Sequence[str], deleted: Sequence[str] = ()
) -> list[tuple[str, str]]:
    """The ``(kind, command)`` checks the changed *paths* call for.

    A kind runs when the integration changed any file of it, *deleted* ones
    included. ``{paths}`` in a command is replaced by the surviving changed
    files of its kind; a ``{paths}`` command with none left to name is skipped,
    while a whole-tree command (no ``{paths}``) still runs.
    """
    checks: list[tuple[str, str]] = []
    for kind, command, suffixes in (
        ("python", config.resolve.python_checks, PYTHON_SUFFIXES),
        ("frontend", config.resolve.frontend_checks, FRONTEND_SUFFIXES),
    ):
        touched = [p for p in paths if p.endswith(suffixes)]
        removed = [p for p in deleted if p.endswith(suffixes)]
        if touched or removed:
            filled = _filled_check(kind, command, touched, removed)
            if filled is not None:
                checks.append((kind, filled))
    return checks


def _filled_check(
    kind: str, command: str, touched: list[str], removed: list[str]
) -> str | None:
    """*command* for a *kind* the integration changed, or None when it is not
    run: none configured, or a ``{paths}`` command whose kind only lost files
    (*removed*), so it has nothing left to name. Both push the kind unchecked
    and say so at WARNING, naming the key."""
    key = f"[resolve] {kind}_checks"
    if not command:
        _logger.warning(
            "resolve: the resolution changes %d %s file(s) but no %s check "
            "is configured — they are pushed unchecked; set %s to check them",
            len(touched) + len(removed),
            kind,
            kind,
            key,
        )
        return None
    if "{paths}" not in command:
        return command
    if not touched:
        _logger.warning(
            "resolve: %s names {paths}, but the resolution only deleted %s "
            "file(s) (%s), so it has none to check — pushed unchecked; drop "
            "{paths} to check the whole tree",
            key,
            kind,
            ", ".join(removed),
        )
        return None
    return command.replace("{paths}", " ".join(shlex.quote(p) for p in touched))


def first_failing_line(evidence: str) -> str:
    """The line of a check's *evidence* a hold reason quotes.

    The first output line that names an error or failure, else the first
    non-empty one; the ``command exited N:`` prefix and the contained run's
    ``stdout:``/``stderr:`` labels are not part of it.
    """
    lines = []
    for raw in _EXIT_PREFIX.sub("", evidence.strip()).splitlines():
        line = _STREAM_LABEL.sub("", raw.strip()).strip()
        if line:
            lines.append(line)
    if not lines:
        return "no output"
    chosen = next((line for line in lines if _FAILURE_WORD.search(line)), lines[0])
    if len(chosen) > _FAILING_LINE_CHARS:
        return chosen[:_FAILING_LINE_CHARS] + "…"
    return chosen


#: Marks a stripped relative path while the rest of its line is sanitized: a
#: private-use character the sanitizer keeps, removed from each line first.
_KEPT = "\ue000"
_KEPT_SLOT = re.compile(_KEPT + r"(\d+)" + _KEPT)
#: A relative path segment kept as printed: no encoding, no assignment.
_PLAIN_SEGMENT = r"[\w.@+(),:\[\]-]+"
#: A directory of a kept path: plain characters and balanced route groups
#: (``(factory)``, ``[id]``, ``[[lang]]``), ending on a word or a close. Any
#: other segment before a ``/`` (``a.ts[``, ``a.ts:3:1``, ``x-``) opens another
#: absolute path, which is the rest of the line's to sanitize, not the tree's.
_ROUTE_GROUP = r"\([\w.@+-]*\)|\[\[[\w.@+-]*\]\]|\[[\w.@+-]*\]"
_DIR_SEGMENT = re.compile(rf"(?:[\w.@+-]|{_ROUTE_GROUP})*(?:\w|{_ROUTE_GROUP})")


def readable_output(output: str, *roots: str) -> str:
    """A check's whole *output* as kew keeps it, each line sanitized like the
    evidence (credentials, URL secrets, path ancestry), except that a path
    beneath one of the *roots* the check ran in is named from the repository
    root: ``frontend/src/lib/fixtures.test.ts``, not ``<path>/fixtures.test.ts``.

    Credentials are redacted before a root is stripped (#3495). A root is
    stripped only where a path token starts with it and only when what follows
    is a plain relative path (no ``.``/``..`` segment, no encoding), which is
    then kept segment by segment, each segment sanitized on its own (so a
    token in a file name is still redacted), while the rest of the line is
    sanitized whole: so
    ``routes/(factory)/work/+page.svelte`` keeps its route and
    ``/work/../home/x`` is still reduced to its basename.
    """
    rooted = re.compile(
        r"(?<![^\s\"'(\[=])(?:"
        + "|".join(re.escape(root) for root in roots)
        + rf")((?:{_PLAIN_SEGMENT}/)*{_PLAIN_SEGMENT})(?=$|[\s\"'\]])"
    )
    return "\n".join(
        _readable_line(raw, rooted if roots else None) if raw.strip() else ""
        for raw in output.splitlines()
    )


def _readable_line(raw: str, rooted: re.Pattern[str] | None) -> str:
    kept: list[str] = []
    line = redact_credentials(raw.replace(_KEPT, ""))
    if rooted is not None:
        line = rooted.sub(partial(_keep_relative, kept), line)
    line = sanitize_publication_evidence(line, limit=None)
    return _KEPT_SLOT.sub(lambda slot: kept[int(slot.group(1))], line)


def _keep_relative(kept: list[str], match: re.Match[str]) -> str:
    parts = match.group(1).split("/")
    cut = next(
        (
            i + 1
            for i, part in enumerate(parts[:-1])
            if not _DIR_SEGMENT.fullmatch(part)
        ),
        len(parts),
    )
    rel = "/".join(parts[:cut])
    rest = "".join("/" + part for part in parts[cut:])
    if not rel or any(part in {".", ".."} for part in rel.split("/")):
        return match.group(0)
    # Each segment still passes the evidence sanitizer, the only one that
    # redacts a GitHub token; alone it holds no "/" to read as ancestry.
    kept.append(
        "/".join(
            sanitize_publication_evidence(part, limit=None) for part in rel.split("/")
        )
    )
    return f"{_KEPT}{len(kept) - 1}{_KEPT}{rest}"


def error_lines(output: str) -> list[str]:
    """The error lines of a check's whole *output*, wherever they fall.

    A svelte-check ``Error:`` line carries the ``path:line:col`` line above
    it. Warnings and summaries are left out: the tail of a failed
    ``npm run check`` is warnings, and quoting them hid the error (#3495).
    """
    found: list[str] = []
    previous = ""
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _ERROR_LINE.search(line) and not line.startswith("Warn"):
            if line.startswith("Error") and _LOCATION_LINE.search(previous):
                line = f"{previous} {line}"
            if len(line) > _FAILING_LINE_CHARS:
                line = line[:_FAILING_LINE_CHARS] + "…"
            if line not in found:
                found.append(line)
        previous = line
    return found


def _failing_excerpt(evidence: str, output: str) -> str:
    """What a hold reason quotes of a red check: the error lines of its
    *output*, else the first failing line of its *evidence*."""
    found = error_lines(output)
    if not found:
        return first_failing_line(evidence)
    quoted = "; ".join(found[:_MAX_ERROR_LINES])
    extra = len(found) - _MAX_ERROR_LINES
    return f"{quoted} (+{extra} more)" if extra > 0 else quoted


#: Appended to a red check that ran in ``[sandbox] check_image``: an image
#: built before main's lock files last moved fails until it is rebuilt.
STALE_CHECK_IMAGE_HINT = (
    " (if main moved a lock file since [sandbox] check_image was built, the "
    "image fails this check until it is rebuilt with "
    "scripts/build-check-image.sh and check_image names the new tag)"
)


def _failure_summary(
    kind: str, result: ConditionResult, output: str, *, check_image: bool = False
) -> str:
    summary = _result_summary(kind, result, output)
    if check_image and not (result.timed_out or result.unrunnable):
        return summary + STALE_CHECK_IMAGE_HINT
    return summary


def _result_summary(kind: str, result: ConditionResult, output: str) -> str:
    key = f"[resolve] {kind}_checks"
    if result.timed_out:
        return f"the resolved tree's {kind} check ({key}) timed out"
    if result.unrunnable:
        return (
            f"the resolved tree's {kind} check ({key}) could not run: "
            f"{first_failing_line(result.evidence)}"
        )
    exited = re.match(r"command exited (-?\d+)", result.evidence)
    code = f", exit {exited.group(1)}" if exited else ""
    return (
        f"the resolved tree fails its {kind} check ({key}{code}): "
        f"{_failing_excerpt(result.evidence, output)}"
    )


def _bounded(text: str) -> str:
    if len(text) <= _MAX_EVIDENCE_TEXT:
        return text
    return text[:_MAX_EVIDENCE_TEXT] + "…"


def resolution_evidence_path(log_file: Path) -> Path:
    """The resolution evidence path beside a run's *log_file*."""
    return log_file.with_name(log_file.stem + RESOLUTION_EVIDENCE_SUFFIX)


def record_resolution_evidence(
    log_file: Path, *, since: str, failure: ResolutionChecksFailed
) -> Path | None:
    """Keep a refused resolution's check output beside its run's log (#3495).

    The check's command and whole sanitized output, against the PR head
    *since*: the hold reason quotes only the error lines. Owner-only and
    ``O_NOFOLLOW``, credentials redacted. Nothing here reads the agent's
    checkout. Returns the path, or None when it could not be written: the
    hold still stands without it.
    """
    payload = {
        "version": 1,
        "since": since,
        "check": {
            "summary": failure.summary,
            "command": failure.command,
            "output": _bounded(failure.output),
        },
    }
    path = resolution_evidence_path(log_file)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(redact_credentials(json.dumps(payload, indent=2)) + "\n")
    except OSError as exc:
        _logger.warning("resolve: resolution evidence not written to %s: %s", path, exc)
        return None
    return path


def _warn_unchecked(worktree: Path, config: DispatchConfig, *, since: str) -> None:
    """Say which kinds a resolution with no check configured pushes unchecked.

    Nothing is gated here, so a tree whose changes cannot be read only loses
    the per-kind count, never the push.
    """
    try:
        resolution_checks(
            config,
            touched_paths(worktree, since=since),
            deleted_paths(worktree, since=since),
        )
    except (ResolutionChecksFailed, OSError, ValueError) as exc:
        _logger.warning(
            "resolve: no [resolve] python_checks or frontend_checks is configured "
            "— the resolution is pushed unchecked (its changes could not be "
            "read: %s)",
            exc,
        )


def run_resolution_checks(
    worktree: Path, config: DispatchConfig, *, since: str
) -> None:
    """Run the node's checks over the resolved *worktree*, or raise.

    Raises :class:`ResolutionChecksFailed` naming the first check that did not
    pass — red, timed out, or with no admitted posture to run under. A kind
    the integration changed with no check configured is logged at WARNING and
    pushed unchecked.
    """
    if not (config.resolve.python_checks or config.resolve.frontend_checks):
        _warn_unchecked(worktree, config, since=since)
        return
    checks = resolution_checks(
        config,
        touched_paths(worktree, since=since),
        deleted_paths(worktree, since=since),
    )
    if not checks:
        return
    # Local imports: the verification stack pulls in the sandbox (heavy).
    from kew.loops.verification import (
        CommandCondition,
        resolve_check_image_admission,
    )
    from kew.sandbox import DEFAULT_IMAGE_RUNNER, WORK_MOUNT_PATH

    admission = resolve_check_image_admission(
        config.sandbox, runner=config.agent.runner or DEFAULT_IMAGE_RUNNER
    )
    for kind, command in checks:
        result = CommandCondition(
            command, timeout=RESOLUTION_CHECK_TIMEOUT_SECONDS
        ).check_admitted_directory(worktree, admission)
        if not result.met:
            output = readable_output(
                result.output,
                f"{worktree}/",
                f"{worktree.resolve()}/",
                _CHECK_IMAGE_TREE,
                f"{WORK_MOUNT_PATH}/",
            )
            raise ResolutionChecksFailed(
                _failure_summary(
                    kind,
                    result,
                    output,
                    check_image=bool(config.sandbox.check_image.strip()),
                ),
                f"`{command}`: {result.evidence}",
                command=command,
                output=output,
            )
        _logger.info("resolve: the resolved tree passes its %s check", kind)
