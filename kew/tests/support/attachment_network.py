"""Answer a container runtime's ``network inspect`` at the process boundary.

A credentialed container launch reads its attachment network's shape and
refuses one it cannot show closed. Tests about something else give the launch
a closed network here, so that rule is satisfied rather than bypassed.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from kew import subprocess_utils

#: The shape the open-egress rule admits without the operator's consent.
INTERNAL_BRIDGE: Mapping[str, object] = {"Driver": "bridge", "Internal": True}

#: A plain bridge, as ``kew sandbox network`` creates it: open egress.
PLAIN_BRIDGE: Mapping[str, object] = {"Driver": "bridge", "Internal": False}


def _answering(
    shape: Mapping[str, object], inspected: list[list[str]]
) -> Callable[..., Any]:
    """A ``subprocess.run`` that answers ``network inspect`` and passes the rest."""
    real_run = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        words = [str(word) for word in argv] if isinstance(argv, list) else []
        if words[1:3] == ["network", "inspect"]:
            inspected.append(words)
            document = json.dumps([dict(shape)])
            return subprocess.CompletedProcess(argv, 0, document, "")
        return real_run(argv, *args, **kwargs)

    return run


@contextmanager
def attachment_network(
    shape: Mapping[str, object] = INTERNAL_BRIDGE, *, runtime: str = "docker"
) -> Iterator[list[list[str]]]:
    """Answer every ``<runtime> network inspect`` with *shape*.

    Yields the inspected argvs. Any other process call goes through unchanged,
    and the runtime binary is registered under a path that does not exist, so
    nothing else reaches a real daemon.
    """
    inspected: list[list[str]] = []
    with (
        patch.dict(subprocess_utils.TOOL_PATHS, {runtime: f"/nonexistent/{runtime}"}),
        patch.object(subprocess, "run", side_effect=_answering(shape, inspected)),
    ):
        yield inspected


def close_attachment_network(
    monkeypatch: pytest.MonkeyPatch,
    shape: Mapping[str, object] = INTERNAL_BRIDGE,
    *,
    runtime: str = "docker",
) -> list[list[str]]:
    """:func:`attachment_network` for the rest of a test, undone by *monkeypatch*."""
    inspected: list[list[str]] = []
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, runtime, f"/nonexistent/{runtime}")
    monkeypatch.setattr(subprocess, "run", _answering(shape, inspected))
    return inspected
