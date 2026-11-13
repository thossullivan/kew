"""Gitignore matching for host extraction, from the BASE commit's rules only.

:func:`kew.publication_extract.extract_on_host` walks an agent checkout without
asking git anything. It still has to leave out what git would leave out:
``npm run check`` writes ``.svelte-kit/``, ``mkdocs build`` writes ``site/``,
and neither is work (#2108). This module answers "would git ignore this
untracked path?" for that walker.

**Where the rules come from.** Only the ``.gitignore`` blobs of the base
commit, read by the host from the canonical repository: the root file and every
nested one the base tree holds as a regular file. Nothing in the agent's
checkout is consulted, so a ``.gitignore`` the agent writes or edits cannot hide
its own work or re-include something; it publishes as an ordinary change.
``.git/info/exclude`` and ``core.excludesFile`` are not read either: both are
per-machine configuration, not content of the base commit.

**Semantics** follow gitignore(5) and git's ``wildmatch`` with
``WM_PATHNAME``:

* one pattern per line; a trailing CR is dropped, blank lines and lines
  starting with ``#`` are skipped; trailing spaces are dropped unless escaped;
* ``!`` negates; ``\\!`` and ``\\#`` are literal;
* a trailing ``/`` matches directories only;
* a pattern with a ``/`` at the start or in the middle is anchored to the
  directory holding its ``.gitignore``; otherwise it matches a name at any
  depth beneath that directory;
* ``*`` and ``?`` do not cross ``/``; ``[...]`` is a bracket expression
  (``!``/``^`` negation, ranges, ``[:class:]``) that never matches ``/``;
* ``**/`` leading, ``/**`` trailing and ``/**/`` in the middle cross
  directories; any other run of stars is a single ``*``;
* deeper ``.gitignore`` files take precedence, and within a file the last
  matching pattern wins;
* a path beneath an ignored directory is ignored, whatever a later negation
  says: git never looks inside an excluded directory.

Git compares an anchored pattern's literal prefix (everything before the first
``*``, ``?``, ``[`` or ``\\``) as a plain string, then hands the rest to
``wildmatch`` together with the prefix's last character, so wildmatch still
sees what precedes a run of stars: in ``foo**/bar`` the ``**`` follows ``o``,
not ``/``, so it is one ``*`` and matches ``fooX/bar`` but not ``fooX/Y/bar``.
This module splits the prefix the same way. Git before late 2025 handed
wildmatch none of the prefix, so there that ``**`` crossed directories; that
was a bug, fixed in ``match_pathname`` ("give fnmatch one char of prefix
context"), and this module follows current, fixed git. An older git (Ubuntu
24.04's, or Debian trixie's in ``docker/agent.Dockerfile``) still crosses
directories for this rare pattern class, so the container's
``--exclude-standard`` producer can keep back a path that host extraction
publishes; that gap closes as those gits update.

**No backtracking.** Patterns come from the trusted base, but the names they
are matched against are chosen by the agent. Matching never builds a regex:
each pattern compiles to tokens, and a match runs them over the SET of
reachable offsets in the name, kept as one integer bitmask (a position-set NFA
simulation). Every token is a constant number of big-integer operations, a
``*`` is ``log2(len(name))`` of them, and nothing recurses, so the cost is
polynomial in the pattern and name lengths whatever the name is.

**Bytes, not characters.** wildmatch works on bytes, so ``?`` is one byte and
``a?`` does not match ``aé`` (``é`` is two bytes in UTF-8). Both the patterns
and the names are handled in byte form (Latin-1 text, one character per byte),
so a non-ASCII bracket member is its bytes too, exactly as in git.

Matching is case-sensitive, as git's is with ``core.ignoreCase`` false (the
default on Linux, where the contained producer runs). A pattern git would treat
as invalid (an unterminated bracket, an unknown class, a trailing lone
backslash) matches nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

__all__ = ["IgnorePattern", "IgnoreRules", "parse_gitignore"]

_LOG = logging.getLogger(__name__)

_GITIGNORE = ".gitignore"

#: A UTF-8 byte order mark, in byte form.
_UTF8_BOM = "\xef\xbb\xbf"


def _as_bytes(text: str) -> str:
    """*text*'s UTF-8 bytes, one character per byte (Latin-1).

    Git matches bytes: ``a?`` does not match ``aé``, whose ``é`` is two bytes.
    A filesystem-decoded name's undecodable bytes (``surrogateescape``) come
    back as the bytes they were.
    """
    return text.encode("utf-8", "surrogateescape").decode("latin-1")


def _as_text(byte_form: str) -> str:
    """The readable text behind :func:`_as_bytes` output, for log messages."""
    return byte_form.encode("latin-1").decode("utf-8", "backslashreplace")


#: The characters that end git's literal prefix (``simple_length``).
_GLOB_SPECIAL = frozenset("*?[\\")

#: ``[:name:]`` classes git's wildmatch accepts, as inclusive byte ranges
#: (ASCII only: git's ctype puts no byte above 0x7f in any class).
_CLASSES: Mapping[str, tuple[tuple[int, int], ...]] = {
    "alnum": ((0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A)),
    "alpha": ((0x41, 0x5A), (0x61, 0x7A)),
    "blank": ((0x09, 0x09), (0x20, 0x20)),
    "cntrl": ((0x00, 0x1F), (0x7F, 0x7F)),
    "digit": ((0x30, 0x39),),
    "graph": ((0x21, 0x7E),),
    "lower": ((0x61, 0x7A),),
    "print": ((0x20, 0x7E),),
    "punct": ((0x21, 0x2F), (0x3A, 0x40), (0x5B, 0x60), (0x7B, 0x7E)),
    "space": ((0x09, 0x0D), (0x20, 0x20)),
    "upper": ((0x41, 0x5A),),
    "xdigit": ((0x30, 0x39), (0x41, 0x46), (0x61, 0x66)),
}

# Token kinds.
_LITERAL = "literal"  # one given character
_ONE = "one"  # `?`: one character other than `/`
_SET = "set"  # `[...]`: one character other than `/` in (or not in) the set
_STAR = "star"  # `*`: any run of characters other than `/`
_ANYTHING = "anything"  # `**` (trailing, or before `\/`): any text, `/` included
_DIRECTORIES = "directories"  # `**/`: zero or more leading directories


class _InvalidPattern(ValueError):
    """A pattern git's wildmatch would abort on; it matches nothing."""


@dataclass(frozen=True)
class _Token:
    kind: str
    char: str = ""
    negated: bool = False
    ranges: tuple[tuple[int, int], ...] = ()

    def accepts(self, char: str) -> bool:
        """For a ``_SET`` token: does it match *char*? Never ``/``."""
        if char == "/":
            return False
        code = ord(char)
        inside = any(low <= code <= high for low, high in self.ranges)
        return inside != self.negated


class _Subject:
    """One name, indexed for the position-set simulation.

    Bit ``i`` of a position set means "the pattern so far matches
    ``name[:i]``"; bits run from 0 to ``len(name)``.
    """

    def __init__(self, text: str) -> None:
        self.size = len(text)
        self.everything = (1 << (self.size + 1)) - 1
        masks: dict[str, int] = {}
        for index, char in enumerate(text):
            masks[char] = masks.get(char, 0) | (1 << index)
        self._masks = masks
        self.slashes = masks.get("/", 0)
        self.not_slash = ((1 << self.size) - 1) & ~self.slashes
        # (k, bits i where name[i:i+k] holds no `/`), for k = 1, 2, 4, ...
        ladder: list[tuple[int, int]] = []
        runs, step = self.not_slash, 1
        while runs and step <= self.size:
            ladder.append((step, runs))
            runs &= runs >> step
            step <<= 1
        self._ladder = tuple(ladder)
        self._set_masks: dict[_Token, int] = {}

    def char_mask(self, char: str) -> int:
        return self._masks.get(char, 0)

    def set_mask(self, token: _Token) -> int:
        cached = self._set_masks.get(token)
        if cached is None:
            cached = 0
            for char, mask in self._masks.items():
                if token.accepts(char):
                    cached |= mask
            self._set_masks[token] = cached
        return cached

    def star(self, reach: int) -> int:
        """Extend every position across any run without ``/``.

        Doubling steps: after step ``k`` every distance below ``2k`` whose
        span is slash-free has been added, so ``log2(len)`` steps cover all.
        """
        for step, runs in self._ladder:
            reach |= (reach & runs) << step
        return reach

    def from_lowest(self, reach: int) -> int:
        """Every position at or after the lowest one in *reach*."""
        return self.everything & ~((reach & -reach) - 1)


@lru_cache(maxsize=512)
def _subject(text: str) -> _Subject:
    return _Subject(text)


def _advance(token: _Token, reach: int, subject: _Subject) -> int:
    """The position set after *token*, given the set before it."""
    if token.kind == _LITERAL:
        return (reach & subject.char_mask(token.char)) << 1
    if token.kind == _ONE:
        return (reach & subject.not_slash) << 1
    if token.kind == _SET:
        return (reach & subject.set_mask(token)) << 1
    if token.kind == _STAR:
        return subject.star(reach)
    if token.kind == _ANYTHING:
        return subject.from_lowest(reach)
    # _DIRECTORIES: skip it (zero directories), or anything then a `/`.
    return reach | ((subject.from_lowest(reach) & subject.slashes) << 1)


def _run(tokens: tuple[_Token, ...], text: str) -> bool:
    """Does the whole of *text* match *tokens*?"""
    if not tokens:
        return not text
    subject = _subject(text)
    reach = 1
    for token in tokens:
        reach = _advance(token, reach, subject)
        if not reach:
            return False
    return bool((reach >> subject.size) & 1)


@dataclass(frozen=True)
class IgnorePattern:
    """One parsed line of a ``.gitignore``."""

    negated: bool
    directory_only: bool
    #: Matched against the basename when the pattern has no inner slash,
    #: otherwise against the path relative to the ``.gitignore``'s directory.
    basename_only: bool
    #: Compared as a plain string before the tokens run (anchored patterns).
    prefix: str
    #: ``None`` for a pattern git cannot parse: it matches nothing.
    tokens: tuple[_Token, ...] | None

    def matches(self, relative: str, *, is_dir: bool) -> bool:
        """Does this pattern match *relative*, given in byte form?

        *relative* must already be :func:`_as_bytes` output (one character per
        UTF-8 byte), as :class:`IgnoreRules` passes it; ASCII is unchanged.
        """
        if self.tokens is None or (self.directory_only and not is_dir):
            return False
        subject = relative.rsplit("/", 1)[-1] if self.basename_only else relative
        if not subject.startswith(self.prefix):
            return False
        return _run(self.tokens, subject[len(self.prefix) :])


def _trim_trailing_spaces(line: str) -> str:
    """Drop unescaped trailing spaces, as git's ``trim_trailing_spaces``."""
    last_space: int | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if char == " ":
            if last_space is None:
                last_space = index
        elif char == "\\":
            index += 1
            if index >= len(line):
                return line
            last_space = None
        else:
            last_space = None
        index += 1
    return line if last_space is None else line[:last_space]


def _bracket_char(pattern: str, index: int) -> tuple[int, int]:
    """One (possibly escaped) character in a bracket; returns (code, next)."""
    if pattern[index] == "\\":
        index += 1
        if index >= len(pattern):
            raise _InvalidPattern("escape at end of bracket")
    return ord(pattern[index]), index + 1


def _bracket_class(
    pattern: str, index: int
) -> tuple[tuple[tuple[int, int], ...], int] | None:
    """A ``[:name:]`` at *index*; returns (its ranges, next index).

    As in wildmatch, the class runs to the first ``]``; when that ``]`` is not
    preceded by ``:`` the ``[`` is an ordinary member (``None``).
    """
    end = pattern.find("]", index + 2)
    if end < 0:
        raise _InvalidPattern("unterminated character class")
    if end - 1 < index + 2 or pattern[end - 1] != ":":
        return None
    name = pattern[index + 2 : end - 1]
    if name not in _CLASSES:
        raise _InvalidPattern(f"unknown character class {name!r}")
    return _CLASSES[name], end + 1


def _bracket_member(pattern: str, index: int, ranges: list[tuple[int, int]]) -> int:
    """Parse one bracket member (class, range or character) into *ranges*."""
    if index >= len(pattern):
        raise _InvalidPattern("unterminated bracket expression")
    if pattern.startswith("[:", index):
        found = _bracket_class(pattern, index)
        if found is not None:
            ranges.extend(found[0])
            return found[1]
    low, index = _bracket_char(pattern, index)
    after_dash = pattern[index + 1 : index + 2]
    if pattern[index : index + 1] == "-" and after_dash not in {"", "]"}:
        high, index = _bracket_char(pattern, index + 1)
        # wildmatch compares the start character on its own before it sees
        # the `-`, so a reversed range such as `[b-a]` still matches `b`.
        ranges.append((low, max(low, high)))
        return index
    ranges.append((low, low))
    return index


def _compile_bracket(pattern: str, index: int) -> tuple[_Token, int]:
    """Compile the bracket expression opening at *index* (the ``[``)."""
    index += 1
    negated = pattern[index : index + 1] in {"!", "^"}
    if negated:
        index += 1
    ranges: list[tuple[int, int]] = []
    # A `]` straight after the opening (or its negation) is a member.
    index = _bracket_member(pattern, index, ranges)
    while pattern[index : index + 1] != "]":
        index = _bracket_member(pattern, index, ranges)
    return _Token(_SET, negated=negated, ranges=tuple(ranges)), index + 1


def _compile_stars(pattern: str, index: int, tokens: list[_Token]) -> int:
    """Compile the run of ``*`` at *index*; returns the next index.

    A run of two or more is ``**`` only when it is a whole segment of the
    pattern wildmatch sees: preceded by its start or ``/``, and followed by
    its end, ``/`` or ``\\/``. Any other run is one ``*``.
    """
    end = index
    while end < len(pattern) and pattern[end] == "*":
        end += 1
    kind, consumed = _star_kind(pattern, index, end)
    tokens.append(_Token(kind))
    return end + consumed


def _star_kind(pattern: str, start: int, end: int) -> tuple[str, int]:
    """The token for ``pattern[start:end]`` (all ``*``), and the separator
    characters after it that the token also consumes.

    Mirrors wildmatch.c: a run of two or more after the pattern start or a
    ``/``, and before the end, a ``/`` or an escaped ``\\/``, crosses
    directories. Only an UNESCAPED ``/`` also takes git's zero-directory
    shortcut (``p[0] == '/'``), so ``**/b`` matches ``b`` but ``**\\/b``
    does not: there the ``**`` matches any text and the escaped slash is an
    ordinary literal after it.
    """
    if end - start < 2 or (start > 0 and pattern[start - 1] != "/"):
        return _STAR, 0
    after = pattern[end : end + 2]
    if after.startswith("/"):
        return _DIRECTORIES, 1
    if not after or after == "\\/":
        return _ANYTHING, 0
    return _STAR, 0


def _compile(pattern: str) -> tuple[_Token, ...]:
    """Compile a wildmatch pattern (``WM_PATHNAME``) to tokens."""
    tokens: list[_Token] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            index = _compile_stars(pattern, index, tokens)
        elif char == "?":
            tokens.append(_Token(_ONE))
            index += 1
        elif char == "[":
            token, index = _compile_bracket(pattern, index)
            tokens.append(token)
        elif char == "\\":
            if index + 1 >= len(pattern):
                raise _InvalidPattern("trailing backslash")
            tokens.append(_Token(_LITERAL, pattern[index + 1]))
            index += 2
        else:
            tokens.append(_Token(_LITERAL, char))
            index += 1
    return tuple(tokens)


def _literal_prefix(pattern: str) -> str:
    """Git's ``simple_length``: the pattern up to its first glob character."""
    for index, char in enumerate(pattern):
        if char in _GLOB_SPECIAL:
            return pattern[:index]
    return pattern


def _parse_line(line: str) -> IgnorePattern | None:
    if not line or line.startswith("#"):
        return None
    line = _trim_trailing_spaces(line)
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    directory_only = line.endswith("/")
    if directory_only:
        line = line[:-1]
    if not line:
        return None
    basename_only = "/" not in line
    if line.startswith("/"):
        line = line[1:]
    # Only an anchored pattern has its literal prefix split off, as in git's
    # match_pathname; a basename pattern is matched whole. Its last character
    # stays with the tokens (git's ``prefix--``), so a run of stars right
    # after the prefix still sees what precedes it: ``foo**`` is one ``*``.
    prefix = "" if basename_only else _literal_prefix(line)[:-1]
    tokens: tuple[_Token, ...] | None
    try:
        tokens = _compile(line[len(prefix) :])
    except _InvalidPattern as exc:
        # Git also skips a pattern it cannot parse; it then matches nothing,
        # so the path stays in scope (fails toward publishing, not hiding).
        _LOG.warning(
            "invalid .gitignore pattern %r matches nothing: %s",
            _as_text(line),
            exc,
        )
        tokens = None
    return IgnorePattern(
        negated=negated,
        directory_only=directory_only,
        basename_only=basename_only,
        prefix=prefix,
        tokens=tokens,
    )


def parse_gitignore(content: bytes) -> tuple[IgnorePattern, ...]:
    """Parse one ``.gitignore`` file's bytes, in file order.

    Decoded as Latin-1, so each character IS one byte: wildmatch's ``?``,
    ``*`` and bracket members work on bytes, and so does this module.
    """
    text = content.decode("latin-1").removeprefix(_UTF8_BOM)
    patterns: list[IgnorePattern] = []
    for raw in text.split("\n"):
        parsed = _parse_line(raw.removesuffix("\r"))
        if parsed is not None:
            patterns.append(parsed)
    return tuple(patterns)


class IgnoreRules:
    """Every base ``.gitignore``, keyed by the directory that holds it."""

    def __init__(self, files: Mapping[str, tuple[IgnorePattern, ...]]) -> None:
        #: directory ("" for the root), in byte form -> its patterns.
        self._by_directory = {
            _as_bytes(directory): patterns
            for directory, patterns in files.items()
            if patterns
        }
        #: Rule directories, deepest first: the order precedence runs in.
        self._deepest_first = sorted(
            self._by_directory,
            key=lambda directory: directory.count("/") + bool(directory),
            reverse=True,
        )
        self._excluded_directories: dict[str, bool] = {}

    @staticmethod
    def gitignore_paths(base_state: Mapping[str, str]) -> Iterable[str]:
        """Base paths that are ``.gitignore`` regular files, in path order.

        A symlinked ``.gitignore`` supplies nothing: git refuses to follow one
        inside the tree.
        """
        return sorted(
            path
            for path, mode in base_state.items()
            if mode in {"100644", "100755"} and path.rsplit("/", 1)[-1] == _GITIGNORE
        )

    def _decide(self, path: str, *, is_dir: bool) -> bool:
        """The last matching pattern's verdict, deepest ``.gitignore`` first.

        Walks the rule directories, not the path's ancestors, so the cost does
        not grow with how deep an agent nests a directory.
        """
        for directory in self._deepest_first:
            if not directory:
                relative = path
            elif path.startswith(directory + "/"):
                relative = path[len(directory) + 1 :]
            else:
                continue
            for pattern in reversed(self._by_directory[directory]):
                if pattern.matches(relative, is_dir=is_dir):
                    return not pattern.negated
        return False

    def _directory_excluded(self, directory: str) -> bool:
        """Is *directory*, or any directory above it, excluded? Memoised.

        Climbs only to the nearest ancestor already decided; the walker meets
        parents before children, so that is usually the direct parent.
        """
        pending: list[str] = []
        current = directory
        excluded = False
        while current:
            cached = self._excluded_directories.get(current)
            if cached is not None:
                excluded = cached
                break
            pending.append(current)
            current = current.rpartition("/")[0]
        for prefix in reversed(pending):
            excluded = excluded or self._decide(prefix, is_dir=True)
            self._excluded_directories[prefix] = excluded
        return excluded

    def is_ignored(self, path: str, *, is_dir: bool) -> bool:
        """Would git ignore *path* (relative to the repository root)?

        *is_dir* must come from ``lstat``: a symlink is never a directory.
        *path* is a filesystem-decoded name; it is matched as its bytes.
        """
        if not self._by_directory:
            return False
        path = _as_bytes(path)
        parent = path.rpartition("/")[0]
        if parent and self._directory_excluded(parent):
            return True
        if is_dir:
            return self._directory_excluded(path)
        return self._decide(path, is_dir=False)
