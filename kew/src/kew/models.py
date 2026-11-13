"""Typed domain models for the GitHub API boundary.

These TypedDicts model the JSON shapes returned by ``gh issue``/``gh pr``
commands. They are used throughout selection, blocking, and tiering so that
governance-critical routing reasons over named fields rather than
``dict[str, Any]``.

TypedDict is intentionally chosen over dataclass: instances remain plain
``dict`` objects at runtime (no constructor overhead, no serialisation
change), so existing callers using ``.get()`` and ``["key"]`` access work
without modification.
"""

from typing import Any, NotRequired, TypedDict


class GhLabel(TypedDict):
    name: str


class GhAssignee(TypedDict):
    login: str


class GhIssue(TypedDict):
    number: int
    title: str
    labels: list[GhLabel]
    body: NotRequired[str | None]
    createdAt: NotRequired[str]
    assignees: NotRequired[list[GhAssignee]]
    url: NotRequired[str | None]
    # TUI routing fields added by tui/state.py after fetch
    _routed_runner: NotRequired[str]
    _routed_model: NotRequired[str]
    # TUI detail-pane flag for Rich-markup bodies (e.g. PR review detail)
    _rich_markup: NotRequired[bool]


class GhPullRequest(TypedDict):
    number: int
    title: str
    author: NotRequired[dict[str, str]]
    createdAt: NotRequired[str]
    comments: NotRequired[list[dict[str, Any]]]
    statusCheckRollup: NotRequired[list[dict[str, Any]]]
