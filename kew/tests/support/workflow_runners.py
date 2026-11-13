"""Resolve a workflow job's ``runs-on`` for a given repository visibility.

The workflows select the self-hosted runner by visibility with one of two
fixed expressions. The runner tests evaluate those expressions, rather than
matching their text, so a rewrite that changes the answer for some visibility
fails even when it still looks plausible.

The evaluator covers the subset of GitHub Actions expression syntax the
workflows use: string, boolean, null and number literals, dotted context
paths with ``.*`` array projection, ``!``, ``==``, ``!=``, ``&&``, ``||``,
parentheses, and the ``contains`` and ``startsWith`` functions. GitHub's
semantics apply: ``&&`` and ``||`` return an operand rather than a boolean,
string equality and the two functions ignore case, and a missing context
property is null. Any other syntax, another function included, raises, so an
unsupported construct can never evaluate to a convenient default.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

HOSTED_RUNNER = "ubuntu-latest"
SELF_HOSTED = "self-hosted"

# The two runner expressions a job may use. The event form reads the webhook
# payload. The resolved form reads a `visibility` job's API answer, because a
# schedule event's payload carries no repository object.
RUNNER_BY_EVENT_VISIBILITY = (
    "${{ github.event.repository.visibility == 'private' "
    "&& 'self-hosted' || 'ubuntu-latest' }}"
)
RUNNER_BY_RESOLVED_VISIBILITY = (
    "${{ needs.visibility.outputs.visibility == 'private' "
    "&& 'self-hosted' || 'ubuntu-latest' }}"
)
ROUTED_RUNNERS = {
    RUNNER_BY_EVENT_VISIBILITY: "github.event.repository.visibility",
    RUNNER_BY_RESOLVED_VISIBILITY: "needs.visibility.outputs.visibility",
}

# Every visibility GitHub reports, plus the two degenerate cases: an empty
# string and an event payload with no repository object at all.
MISSING = object()
NON_PRIVATE_VISIBILITIES: tuple[object, ...] = ("public", "internal", "", MISSING)

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<string>'(?:[^']|'')*')"
    r"|(?P<number>-?\d+(?:\.\d+)?)"
    r"|(?P<op>==|!=|&&|\|\||!|\(|\)|,)"
    r"|(?P<path>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_*][A-Za-z0-9_-]*)*)"
    r")"
)


class UnsupportedExpression(ValueError):
    """The expression uses syntax this evaluator does not model."""


def _tokens(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    source = source.rstrip()
    while position < len(source):
        match = _TOKEN.match(source, position)
        if match is None or match.end() == position:
            raise UnsupportedExpression(f"cannot tokenize {source[position:]!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _lookup(context: dict[str, Any], path: str) -> Any:
    lowered = path.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    return _walk(context, path.split("."))


def _walk(value: Any, parts: list[str]) -> Any:
    for index, part in enumerate(parts):
        if part == "*":
            if not isinstance(value, list):
                return None
            return [_walk(item, parts[index + 1 :]) for item in value]
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
        if value is MISSING:
            return None
    return value


def _truthy(value: Any) -> bool:
    return value not in (None, False, "", 0)


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return bool(left == right)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    raise UnsupportedExpression(f"cannot coerce {value!r} to a string")


def _contains(search: Any, item: Any) -> bool:
    if isinstance(search, list):
        return any(_equal(element, item) for element in search)
    return _as_text(item).lower() in _as_text(search).lower()


def _starts_with(value: Any, prefix: Any) -> bool:
    return _as_text(value).lower().startswith(_as_text(prefix).lower())


_FUNCTIONS = {"contains": _contains, "startswith": _starts_with}


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], context: dict[str, Any]):
        self.tokens = tokens
        self.index = 0
        self.context = context

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise UnsupportedExpression("unexpected end of expression")
        self.index += 1
        return token

    def parse(self) -> Any:
        value = self._or()
        if self._peek() is not None:
            raise UnsupportedExpression(f"trailing tokens {self.tokens[self.index :]}")
        return value

    def _or(self) -> Any:
        value = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._equality()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self._equality()
            value = right if _truthy(value) else value
        return value

    def _equality(self) -> Any:
        value = self._unary()
        while self._peek() in (("op", "=="), ("op", "!=")):
            _, operator = self._take()
            right = self._unary()
            same = _equal(value, right)
            value = same if operator == "==" else not same
        return value

    def _unary(self) -> Any:
        if self._peek() == ("op", "!"):
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        kind, text = self._take()
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return float(text)
        if kind == "path":
            if self._peek() == ("op", "("):
                return self._call(text)
            return _lookup(self.context, text)
        if (kind, text) == ("op", "("):
            value = self._or()
            if self._take() != ("op", ")"):
                raise UnsupportedExpression("unbalanced parenthesis")
            return value
        raise UnsupportedExpression(f"unexpected token {text!r}")

    def _call(self, name: str) -> Any:
        function = _FUNCTIONS.get(name.lower())
        if function is None:
            raise UnsupportedExpression(f"function call {name}(...)")
        self._take()
        arguments = [self._or()]
        while self._peek() == ("op", ","):
            self._take()
            arguments.append(self._or())
        if self._take() != ("op", ")"):
            raise UnsupportedExpression(f"unbalanced call to {name}")
        if len(arguments) != 2:
            raise UnsupportedExpression(f"{name} takes two arguments")
        return function(*arguments)


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate a bare expression, or one wrapped in ``${{ }}``."""
    source = expression.strip()
    if source.startswith("${{") and source.endswith("}}"):
        source = source[3:-2]
    elif "${{" in source:
        raise UnsupportedExpression(f"mixed literal and expression {expression!r}")
    return _Parser(_tokens(source), context).parse()


def context_for(visibility: object) -> dict[str, Any]:
    """The contexts a runner expression can read, for one visibility.

    The resolved form's `visibility` job returns the same value the payload
    would carry, so both sources are set together.
    """
    repository: dict[str, Any] = {}
    if visibility is not MISSING:
        repository["visibility"] = visibility
    output = "" if visibility is MISSING else visibility
    return {
        "github": {"event": {"repository": repository} if repository else {}},
        "needs": {"visibility": {"outputs": {"visibility": output}}},
    }


def resolve_minutes(value: object, visibility: object) -> int:
    """A ``timeout-minutes`` value, literal or expression, for one visibility.

    Anything that does not resolve to a whole number of minutes raises, so a
    limit the evaluator cannot read is never taken as some default.
    """
    if isinstance(value, str) and "${{" in value:
        value = evaluate(value, context_for(visibility))
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or value != int(value)
    ):
        raise UnsupportedExpression(f"timeout-minutes {value!r} is not whole minutes")
    return int(value)


def _labels(value: object, context: dict[str, Any]) -> frozenset[str]:
    if isinstance(value, str):
        if "${{" in value:
            resolved = evaluate(value, context)
            if not isinstance(resolved, str):
                raise UnsupportedExpression(
                    f"runs-on {value!r} resolved to non-string {resolved!r}"
                )
            return frozenset({resolved.strip()})
        return frozenset({value.strip()})
    if isinstance(value, list):
        labels: set[str] = set()
        for label in value:
            labels |= _labels(label, context)
        return frozenset(labels)
    if isinstance(value, dict):
        return _labels(value.get("labels") or [], context) | (
            _labels(value["group"], context) if "group" in value else frozenset()
        )
    return frozenset()


def resolve_runner(job: dict, visibility: object) -> frozenset[str]:
    """The labels ``job`` requests when the repository has ``visibility``."""
    return _labels(job.get("runs-on"), context_for(visibility))


def can_use_self_hosted(job: dict) -> bool:
    """True when the job reaches the self-hosted runner in a private repository."""
    return SELF_HOSTED in resolve_runner(job, "private")


def workflow_paths(directory: Path = WORKFLOWS_DIR) -> list[Path]:
    """Every workflow file, both extensions GitHub reads."""
    return sorted(
        [*directory.glob("*.yml"), *directory.glob("*.yaml")],
        key=lambda path: path.name,
    )
