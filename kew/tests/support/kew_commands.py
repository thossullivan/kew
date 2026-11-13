"""Find the kew commands a piece of agent guidance names, and check them.

Guidance an agent follows (the operate-kew skill, the AGENTS.md block) must only
name commands that ship. These helpers read every ``kew ...`` invocation out of
a Markdown text's code spans and fenced blocks and resolve it against the live
``kew schema`` output, flags included.
"""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_SPAN = re.compile(r"`([^`\n]+)`")
_INVOCATION = re.compile(r"(?<![\w./-])kew((?: +[^\s|#\"')]+)+)")
_QUOTED = re.compile(r'"[^"]*"')
_WORD = re.compile(r"[a-z][a-z0-9-]*")
_FLAG = re.compile(r"--?[a-z][a-z0-9-]*")


def schema_commands() -> dict[tuple[str, ...], set[str]]:
    """Every command path ``kew schema`` prints, mapped to its option flags."""
    from kew.cli import app

    result = CliRunner().invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    return {
        tuple(entry["path"]): {
            flag for option in entry["options"] for flag in option["flags"]
        }
        for entry in document["commands"]
    }


def named_invocations(markdown: str) -> list[list[str]]:
    """The token lists of every ``kew ...`` invocation in code in *markdown*."""
    code = list(_FENCE.findall(markdown))
    prose = _FENCE.sub("", markdown)
    code.extend(_SPAN.findall(prose))
    invocations = []
    for chunk in code:
        for line in chunk.splitlines():
            # A quoted flag value stands in as one token, so flags after it count.
            for match in _INVOCATION.finditer(_QUOTED.sub("VALUE", line)):
                invocations.append(match.group(1).split())
    return invocations


def unknown_invocations(markdown: str) -> list[str]:
    """Each invocation in *markdown* that ``kew schema`` does not describe.

    An invocation resolves to the longest leading run of words that is a
    command path; it is unknown when no prefix is one, or when it passes a flag
    that command does not take.
    """
    commands = schema_commands()
    unknown = []
    for tokens in named_invocations(markdown):
        words = []
        for token in tokens:
            if not _WORD.fullmatch(token):
                break
            words.append(token)
        path = next(
            (
                tuple(words[:n])
                for n in range(len(words), 0, -1)
                if tuple(words[:n]) in commands
            ),
            None,
        )
        flags = [t.split("=", 1)[0] for t in tokens if _FLAG.fullmatch(t.split("=")[0])]
        if path is None or any(flag not in commands[path] for flag in flags):
            unknown.append("kew " + " ".join(tokens))
    return unknown
