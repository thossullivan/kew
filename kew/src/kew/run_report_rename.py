"""Move a renamed repository's run reports to its new sink directory (#3527).

``kew rename-repo --apply`` moves ``<sink>/<old-slug>/*.md`` to
``<sink>/<new-slug>/`` after the ledger commits. Nothing is exported again: a
re-export renders the report from today's ledger, which drops evidence ids the
fetch cap no longer reaches, restates a run's status, removes keys an indexer
added (``permalink``) and fills a missing repo from the config. The move
changes two fields and keeps every other byte:

* ``repo`` when it names the old slug;
* ``pr_url`` when it starts with the old repository's pull request prefix.

Each field is rewritten where ``render_report`` writes it: the frontmatter
line (``repo: <value>``) and the whole body line that renders it
(``- **repo**: <value>``), in the value's own quoting. Any other body line is
left as it is, prose that names the old slug included: once another repository
takes the old name, a body link that still named it would open that
repository's pull request.

No report field hashes these lines: the integrity fields are counts and flag
names read back as text (:func:`kew.run_report_sink._carried_integrity`).

Each move writes the rewritten report to the target with an exclusive create,
then removes the source, so an interruption leaves either the source alone or
both files. A rerun removes a source whose target already holds its rewritten
form, finishes a move an older kew left as two links to one file, and names
every other conflict instead of overwriting anything.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from kew.run_report_sink import _MAX_REPORT_BYTES, _escape_yaml_forbidden
from kew.runner_fields import exception_text

#: A frontmatter line naming one of the two rewritten keys.
_FRONTMATTER_LINE = re.compile(
    r"^(?P<head>(?P<key>repo|pr_url):[ \t]+)(?P<value>.*?)(?P<tail>[ \t]*)$"
)
#: A whole body line rendering one of them, as ``render_report`` writes it.
_BODY_LINE = re.compile(
    r"^(?P<head>- \*\*(?P<key>repo|pr_url)\*\*:[ \t]+)(?P<value>.*?)(?P<tail>[ \t]*)$"
)
#: One complete JSON string token, as ``json.dumps`` writes it.
_JSON_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"')


@dataclass(frozen=True)
class RenamedValues:
    """The old and new value of each rewritten frontmatter field."""

    from_repo: str
    to_repo: str
    #: The pull request link prefix of each repository on its forge.
    old_prefix: str
    new_prefix: str

    def rewrite(self, key: str, value: str) -> str | None:
        """*value*'s new form, or None when it is not the old repository's."""
        if key == "repo":
            return self.to_repo if value == self.from_repo else None
        if value.startswith(self.old_prefix):
            return self.new_prefix + value[len(self.old_prefix) :]
        return None


@dataclass
class RepoReportMove:
    """What :func:`move_repo_reports` did with each report."""

    moved: list[Path] = field(default_factory=list)
    #: Each report left where it was, and why.
    kept: list[str] = field(default_factory=list)


def _decode(raw: str) -> tuple[str, str] | None:
    """A scalar's value and quoting style (``json``, ``single``, ``bare``).

    Only one complete JSON string token reads as ``json``. Any other text
    that opens with a double quote is not a value kew rendered (prose after a
    quoted slug, say): None, and the line is left as it is, since it cannot
    equal the old slug or link anyway.
    """
    if _JSON_STRING.fullmatch(raw):
        return json.loads(raw), "json"
    if raw.startswith('"'):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'"), "single"
    return (raw, "bare") if raw else None


def _encode(value: str, style: str) -> str:
    if style == "json":
        return _escape_yaml_forbidden(json.dumps(value, ensure_ascii=False))
    if style == "single":
        return "'" + value.replace("'", "''") + "'"
    return value


def _rewrite_line(line: str, pattern: re.Pattern[str], values: RenamedValues) -> str:
    """*line* with its value rewritten when the whole line is a renamed field
    *pattern* matches and its value is the old repository's, else as is."""
    text = line.rstrip("\r\n")
    match = pattern.match(text)
    decoded = _decode(match["value"]) if match else None
    if match is None or decoded is None:
        return line
    new = values.rewrite(match["key"], decoded[0])
    if new is None:
        return line
    moved = match["head"] + _encode(new, decoded[1]) + match["tail"]
    return moved + line[len(text) :]


def rewrite_report(content: str, values: RenamedValues) -> str | None:
    """*content* with its ``repo`` and ``pr_url`` fields moved to the new
    repository, every other byte kept; None with no frontmatter.

    The fields are the frontmatter values and the body bullets that render
    them (``- **repo**: <value>``); any other body line, prose naming the old
    slug included, is left as it is.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            head = [_rewrite_line(x, _FRONTMATTER_LINE, values) for x in lines[1:index]]
            body = [_rewrite_line(x, _BODY_LINE, values) for x in lines[index + 1 :]]
            return "".join([lines[0], *head, lines[index], *body])
    return None


def _read(path: Path) -> tuple[str, os.stat_result]:
    """A regular file's text, never through a symlink; raises ValueError."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_REPORT_BYTES:
            raise ValueError("not a regular report file")
        data = os.read(fd, _MAX_REPORT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _MAX_REPORT_BYTES:
        raise ValueError("larger than a report")
    return data.decode("utf-8"), info


def _staged(target: Path, content: str) -> Path:
    """A temp file beside *target* holding *content*, flushed to disk."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return Path(tmp)


def _create(target: Path, content: str, *, replace: bool = False) -> None:
    """Write *target* whole: by exclusive link, or by replace when *replace*."""
    tmp = _staged(target, content)
    try:
        if replace:
            os.replace(tmp, target)
        else:
            os.link(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _move_one(source: Path, target: Path, values: RenamedValues) -> str | None:
    """Move one report; why it stayed, or None once it moved."""
    if not stat.S_ISREG(os.lstat(source).st_mode):
        return f"{source}: not a regular file"
    content, info = _read(source)
    rewritten = rewrite_report(content, values)
    if rewritten is None:
        return f"{source}: no frontmatter to rewrite"
    if not os.path.lexists(target):
        _create(target, rewritten)
    elif (os.lstat(target).st_ino, os.lstat(target).st_dev) == (
        info.st_ino,
        info.st_dev,
    ):
        # An older kew linked the source to the target and stopped there.
        _create(target, rewritten, replace=True)
    elif _read(target)[0] != rewritten:
        return f"{source}: {target} already exists and differs"
    os.unlink(source)
    return None


def move_repo_reports(
    source_dir: Path,
    target_dir: Path,
    values: RenamedValues,
    *,
    only: Collection[str] | None = None,
) -> RepoReportMove:
    """Move every report in *source_dir* to *target_dir*, rewriting two values.

    With *only*, a report moves only when its run id (the file stem) is in it;
    the rest are kept and named. Two names for one directory (a case-only
    rename on a file system that ignores case, or a link) move nothing, since
    removing each source would remove the report itself.

    Idempotent: a moved report is not in *source_dir* to move again, and a
    report the move cannot take is kept and named, never overwritten.
    """
    result = RepoReportMove()
    if not source_dir.is_dir():
        return result
    target_dir.mkdir(parents=True, exist_ok=True)
    if os.path.samefile(source_dir, target_dir):
        result.kept.append(f"{source_dir}: the same directory as {target_dir}")
        return result
    for source in sorted(source_dir.glob("*.md")):
        if only is not None and source.stem not in only:
            result.kept.append(
                f"{source}: not a run of this ledger (with --from-name-reused,"
                " only this ledger's runs move)"
            )
            continue
        target = target_dir / source.name
        try:
            problem = _move_one(source, target, values)
        except (OSError, ValueError) as exc:
            problem = f"{source}: {exception_text(exc)}"
        if problem is None:
            result.moved.append(target)
        else:
            result.kept.append(problem)
    return result
