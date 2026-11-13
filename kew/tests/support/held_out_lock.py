"""A ``kew/uv.lock`` for a tiny held-out repository (#3194).

The held-out and contract runs load only what the base's ``kew/uv.lock`` pins,
at the version it pins, so a repository verified end to end needs one: this
interpreter's own pytest and pluggy, and whatever else a case names. ``uv
sync`` in the job reads it too, so it must be a lock uv accepts.
"""

from __future__ import annotations

from importlib.metadata import version


def pinned_lock(*names: str, **versions: str) -> str:
    """A ``uv.lock`` pinning pytest, pluggy and *names* as installed here.

    *versions* overrides a name's version (``pluggy="0.0.1"``): a lock that
    moves that pin.
    """
    entries = "".join(
        f'[[package]]\nname = "{name}"\n'
        f'version = "{versions.get(name.replace("-", "_"), version(name))}"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        for name in dict.fromkeys(("pytest", "pluggy", *names))
    )
    return f'version = 1\nrequires-python = ">=3.11"\n\n{entries}'
