"""A host that has a container runtime, against the suite's 'none' default.

``tests/conftest.py`` answers every runtime probe with "not found" so a real
docker on the test host cannot flip a test onto the sandboxed path. A test whose
subject runs *inside* a container (with the launch itself faked at the process
boundary) restates the host here: the probe finds the runtime at *path*.
"""

from __future__ import annotations

import pytest

from kew import sandbox
from support.image_resolution import built_by_kew


def runtime_present(monkeypatch: pytest.MonkeyPatch, path: str = "/bin/docker") -> None:
    """The host's runtime probe finds a container runtime at *path*."""
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: path)


def image_resolves_to(monkeypatch: pytest.MonkeyPatch, image_id: str) -> None:
    """The runtime's ``image inspect`` resolves every image to *image_id*.

    Every image is one kew built: a pin tag carries the inputs label its tag
    names, as :func:`support.image_resolution.built_by_kew` answers.
    """
    monkeypatch.setattr(
        sandbox,
        "inspect_image_id",
        lambda _runtime, image: built_by_kew(image, image_id),
    )
