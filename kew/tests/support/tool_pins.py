"""The exact ``uvx`` tool versions kew's gates run, and the calls that run them.

``kew/scripts/tool-pins.env`` holds one ``<PACKAGE>_VERSION=<version>`` line
per tool a gate installs to run (``uvx``, ``uv tool run``, ``uv run --with``).
This module reads it the way bash sources it, reads every such call out of a
script, a workflow step, an image recipe or a Markdown page, and finds every
line that sets a pinned name, so a test can hold each call to the pin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

KEW_DIR = Path(__file__).resolve().parents[2]
TOOL_PINS_ENV = KEW_DIR / "scripts" / "tool-pins.env"

_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=([A-Za-z0-9.+-]+)$")


def _word(word: str) -> str:
    """``word`` as a command word: bare, or quoted whole (`"uvx"` runs uvx)."""
    return rf"(?:\"{word}\"|'{word}'|{word})"


# `uv` and any of its global options before the subcommand: `uv -q run`,
# `uv --directory kew run`. An option may take one value word.
_UV = r"(?<![\w./-])" + _word("uv") + r"(?:[ \t]+-\S+(?:[ \t]+[^\s-]\S*)?)*?[ \t]+"
# `uvx` or `uv tool run` ANYWHERE outside a comment line, not only where a
# command starts: behind `NO_COLOR=1`, `env`, `timeout 60` or `exec` it runs the
# same tool. The words before it decide only whether it runs at all (see
# _NOT_RUN). Whatever it runs without --from is a package it installs.
_TOOL_RUN = re.compile(
    rf"(?<![\w./-])(?:{_word('uvx')}|{_UV}{_word('tool')}[ \t]+{_word('run')})(?=\s)"
)
# `uv run`: it installs only what --with adds to the project environment.
_UV_RUN = re.compile(rf"{_UV}{_word('run')}(?=\s)")
# `uv tool install`, how an image recipe installs a tool; read only where a
# caller asks for it (a release step also installs the wheel it just built).
_TOOL_INSTALL = re.compile(rf"{_UV}{_word('tool')}[ \t]+{_word('install')}(?=\s)")
# Commands that name `uvx` without running it (`unset -f uv uvx`).
_NOT_RUN = {"unset", "which", "type", "hash", "echo", "printf"}
# Where the simple command holding a match starts.
_COMMAND_START = re.compile(r"(?:^|[;&|(`]|\$\()\s*", re.M)
# One shell word, quoted parts and all (`semgrep=="${V}"`); a word ends at
# whitespace, a list operator, a closing paren or a Markdown backtick.
_WORD = re.compile(r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'|;&)`])+")
_QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")
# Options whose value installs a package (comma-separated lists allowed);
# `-w` is uv's short --with.
_OPTIONS_WITH_A_SPEC = {"--from", "--with", "-w"}
# Options of uvx / uv run / uv tool install whose value is not a package.
_OPTIONS_WITH_A_VALUE = {
    *_OPTIONS_WITH_A_SPEC,
    "--python",
    "-p",
    "--index",
    "--default-index",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--index-strategy",
    "--keyring-provider",
    "--allow-insecure-host",
    "--with-requirements",
    "--with-editable",
    "--directory",
    "--project",
    "--package",
    "--extra",
    "--group",
    "--only-group",
    "--no-group",
    "--env-file",
    "--constraints",
    "-c",
    "--resolution",
    "--prerelease",
    "--fork-strategy",
    "--exclude-newer",
    "--color",
    "--cache-dir",
    "--config-file",
    "--config-setting",
    "-C",
    "--python-preference",
    "--python-platform",
    "--link-mode",
    "--refresh-package",
    "--upgrade-package",
    "-P",
    "--reinstall-package",
    "--no-build-package",
    "--no-binary-package",
}
# Short options above whose value may be attached: `-wpip-licenses`.
_ATTACHABLE = {option for option in _OPTIONS_WITH_A_VALUE if len(option) == 2}
# Options that take no value. Any option in neither set is refused: its value,
# if it takes one, could be read as the command and hide a later --with.
_FLAGS = {
    "-q",
    "--quiet",
    "-v",
    "--verbose",
    "-n",
    "--no-cache",
    "--offline",
    "--native-tls",
    "--no-progress",
    "--no-config",
    "--preview",
    "--frozen",
    "--locked",
    "--no-sync",
    "--isolated",
    "--no-project",
    "--active",
    "--no-active",
    "--exact",
    "--inexact",
    "--all-extras",
    "--all-groups",
    "--all-packages",
    "--no-default-groups",
    "--dev",
    "--no-dev",
    "--no-editable",
    "--no-env-file",
    "-s",
    "--script",
    "-m",
    "--module",
    "--gui-script",
    "--refresh",
    "-U",
    "--upgrade",
    "--reinstall",
    "--force",
    "--compile-bytecode",
    "--no-build-isolation",
    "--no-build",
    "--no-binary",
    "--managed-python",
    "--no-managed-python",
    "--no-python-downloads",
}
_SPEC = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(.+)$")
_VARIABLE = re.compile(r"^\$\{?([A-Z][A-Z0-9_]*)\}?$")
_SOURCE = re.compile(r"^\s*(?:source|\.)\s+(\S*tool-pins\.env)\s*$", re.M)
# The read a script that sources nothing uses, as scan-agent-image.sh reads
# trivy.pin: NAME="$(awk -F= '$1 == "NAME" { print $2 }' path/tool-pins.env)".
_AWK_READ = re.compile(
    r"^\s*(\w+)=\"\$\(awk -F= '\$1 == \"(\w+)\" \{ print \$2 \}' "
    r"(\S*tool-pins\.env)\)\"\s*$",
    re.M,
)


def tool_pins(path: Path = TOOL_PINS_ENV) -> dict[str, str]:
    """Every pin in the file; anything but a comment or plain assignment fails."""
    pins: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        assert match, f"{path.name}:{number} is not a plain assignment: {line!r}"
        pins[match.group(1)] = match.group(2)
    return pins


def pin_name(package: str) -> str:
    """The pin file's name for a package.

    ``mkdocs-material`` → ``MKDOCS_MATERIAL_VERSION``.
    """
    return re.sub(r"[^A-Z0-9]", "_", package.upper()) + "_VERSION"


@dataclass(frozen=True)
class UvxCall:
    """One ``uvx`` call: the text it was read from and each package it installs."""

    where: str
    command: str
    specs: tuple[str, ...]
    #: Options before the command the scan does not know, so cannot step over.
    unknown: tuple[str, ...] = ()


def _unquote(word: str) -> str:
    return _QUOTED.sub(lambda quoted: quoted.group(1) or quoted.group(2) or "", word)


def _words(rest: str) -> list[str]:
    """The words up to the end of the line, ``--opt=value`` and ``-wvalue``
    split in two."""
    words: list[str] = []
    for word in _WORD.findall(rest.split("\n", 1)[0]):
        word = _unquote(word)
        option, equals, value = word.partition("=")
        if equals and option in _OPTIONS_WITH_A_VALUE:
            words.extend((option, value))
        elif word[:2] in _ATTACHABLE and len(word) > 2:
            words.extend((word[:2], word[2:]))
        else:
            words.append(word)
    return words


def _specs(rest: str, *, runs_a_package: bool) -> UvxCall:
    """The packages ``uvx <rest>`` installs, the command it runs, and any
    option before that command the scan cannot step over (``where`` unset).

    ``runs_a_package`` is false for ``uv run``, whose command comes from the
    project environment rather than a package it installs.
    """
    words = _words(rest)
    specs: list[str] = []
    unknown: list[str] = []
    has_from = not runs_a_package
    while words:
        word = words.pop(0)
        if word == "--":
            word = words.pop(0) if words else ""
        elif word in _OPTIONS_WITH_A_VALUE and words:
            value = words.pop(0)
            if word in _OPTIONS_WITH_A_SPEC:
                specs.extend(spec for spec in value.split(",") if spec)
                has_from = has_from or word == "--from"
            continue
        elif word.startswith("-"):
            if word not in _FLAGS:
                unknown.append(word)
            continue
        if word and not has_from:
            specs.append(word)
        return UvxCall("", word, tuple(specs), tuple(unknown))
    return UvxCall("", "", tuple(specs), tuple(unknown))


def _runs(body: str, start: int) -> bool:
    """Whether the match at ``start`` runs: not an argument of ``unset`` etc."""
    begins = [found.end() for found in _COMMAND_START.finditer(body, 0, start)]
    words = body[begins[-1] if begins else 0 : start].split()
    if words and words[0] in _NOT_RUN:
        return False
    return words[:2] not in (["command", "-v"], ["command", "-V"])


def uvx_calls(text: str, where: str, *, installs: bool = False) -> list[UvxCall]:
    """Every call in ``text`` that installs a package to run, comment lines
    left out: ``uvx``, ``uv tool run``, the ``--with`` of ``uv run``, and with
    ``installs`` also ``uv tool install``."""
    joined = text.replace("\\\n", " ")
    lines = [line for line in joined.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(lines)
    patterns = [(_TOOL_RUN, True), (_UV_RUN, False)]
    if installs:
        patterns.append((_TOOL_INSTALL, True))
    # One call per `uv`: `uv -q tool run x` also reads as `uv run` with `tool`
    # taken for the value of `-q`, so the tool-running reading wins.
    found = sorted(
        (match.start(), not runs_a_package, match.end(), runs_a_package)
        for pattern, runs_a_package in patterns
        for match in pattern.finditer(body)
    )
    calls = []
    seen: set[int] = set()
    for start, _, end, runs_a_package in found:
        if start in seen or not _runs(body, start):
            continue
        seen.add(start)
        call = _specs(body[end:], runs_a_package=runs_a_package)
        calls.append(replace(call, where=where))
    return calls


def pin_assignments(text: str, pins: dict[str, str], where: str) -> list[str]:
    """Each line of ``text`` that sets a pinned name to anything but the pin.

    A pin is set in tool-pins.env and nowhere else: a script or workflow step
    may only read one there with the awk shape _AWK_READ matches, or pass one
    on under its own name (``--build-arg "NAME=${NAME}"``). A
    ``NAME=6.1.0`` after ``source`` would run another version past every
    check on the call itself.
    """
    joined = text.replace("\\\n", " ")
    problems = []
    for line in joined.splitlines():
        if line.lstrip().startswith("#"):
            continue
        awk = _AWK_READ.match(line)
        for name in pins:
            if awk and awk.group(1) == awk.group(2) == name:
                continue
            # `NAME=`, `NAME+=`, and the `${NAME:=x}` / `${NAME=x}` defaults.
            for match in re.finditer(rf"(?<![\w$]){name}([+:]?)=", line):
                # Exactly the pin's own reference, optionally quoted, ending
                # the word (or the quoted word `"NAME=${NAME}"` it sits in).
                passed_on = re.match(
                    rf"(?:\"\$(?:\{{{name}\}}|{name})\"|\$(?:\{{{name}\}}|{name})\"?)"
                    r"(?=[\s;&|)]|$)",
                    line[match.end() :],
                )
                if match.group(1) or not passed_on:
                    problems.append(f"{where}: sets {name}: {line.strip()}")
            other = (
                rf"\bread\b(?:\s+-\w+)*(?:\s+\w+)*\s+{name}\b",
                rf"\bprintf\s+-v\s*{name}\b",
                rf"^\s*ENV\s+{name}\s",
            )
            if any(re.search(pattern, line) for pattern in other):
                problems.append(f"{where}: sets {name}: {line.strip()}")
    return problems


def pin_reads(text: str) -> dict[str, list[str]]:
    """Where ``text`` loads each pin from: name → the ``tool-pins.env`` paths.

    A ``source`` line loads every pin, recorded under ``*``; an awk read loads
    the one name it both assigns and selects.
    """
    reads: dict[str, list[str]] = {"*": _SOURCE.findall(text)}
    for assigned, selected, path in _AWK_READ.findall(text):
        if assigned == selected:
            reads.setdefault(assigned, []).append(path)
    return reads


def pins_read(call: UvxCall) -> list[str]:
    """Each pin name a call's packages take from a shell variable."""
    names = []
    for spec in call.specs:
        match = _SPEC.match(spec)
        variable = _VARIABLE.match(match.group(2)) if match else None
        if variable:
            names.append(variable.group(1))
    return names


def pin_problems(call: UvxCall, pins: dict[str, str], *, literal: bool) -> list[str]:
    """Why a call's packages are not each pinned to the file's exact version.

    ``literal`` refuses a ``${NAME_VERSION}`` reference: a command a
    contributor copies out of a page has no pin file sourced around it.
    Without it a call must read the pin: a gate's literal, even one equal to
    the pin today, is a copy the next pin bump leaves behind.
    """
    problems = [
        f"{call.where}: `{option}` is an option the pin check cannot step over "
        "(its value could hide a later --with); list it in tests/support/tool_pins.py"
        for option in call.unknown
    ]
    for spec in call.specs:
        match = _SPEC.match(spec)
        if match is None:
            problems.append(f"{call.where}: `{spec}` is not an exact `==` version")
            continue
        package, version = match.groups()
        name = pin_name(package)
        if name not in pins:
            problems.append(f"{call.where}: {package} has no {name} in tool-pins.env")
            continue
        variable = _VARIABLE.match(version)
        if variable and literal:
            problems.append(f"{call.where}: `{spec}` must name the version literally")
        elif variable and variable.group(1) != name:
            problems.append(
                f"{call.where}: `{spec}` reads ${variable.group(1)}, not {name}"
            )
        elif not variable and not literal:
            problems.append(
                f"{call.where}: `{spec}` copies a version; read ${{{name}}} instead"
            )
        elif not variable and version != pins[name]:
            problems.append(
                f"{call.where}: `{spec}` is not {name}={pins[name]} from tool-pins.env"
            )
    return problems
