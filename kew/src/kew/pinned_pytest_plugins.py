"""The pytest plugins kew loads into a run it judges, and nothing else (#3194).

A run kew reads a verdict off — the merge verify's held-out and contract runs,
an acceptance author's red run — has plugin autoload switched off
(``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1``), so no ``pytest11`` entry point loads of
its own accord. kew's report plugin (:mod:`kew.held_out_report_plugin`) is
loaded first, from a file kew writes outside the checkout under a name drawn
at random, and the pinned plugins after it by name.

``-p NAME`` resolves through any distribution's ``pytest11`` entry point of
that name, so a name is never trusted on its own: the report plugin refuses
the run unless every plugin and hook that ran is code from a file of one of
the distributions below — at the version the BASE's ``uv.lock`` pins, still
hashing to its ``RECORD`` — or from the base's own test tree. The head's lock
must carry each pin exactly as the base's does (version, source, hashes), or
the run is refused whatever it reports.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PinnedPlugin:
    """One plugin: its entry-point name, the module it must be, its package."""

    name: str
    module: str
    distribution: str


#: What kew's own suite needs: xdist for the parallel phase, pytest-cov,
#: pytest-asyncio and anyio's plugin for its async tests.
PINNED_PYTEST_PLUGINS: tuple[PinnedPlugin, ...] = (
    PinnedPlugin("xdist", "xdist.plugin", "pytest-xdist"),
    PinnedPlugin("pytest_cov", "pytest_cov.plugin", "pytest-cov"),
    PinnedPlugin("asyncio", "pytest_asyncio.plugin", "pytest-asyncio"),
    PinnedPlugin("anyio", "anyio.pytest_plugin", "anyio"),
)

#: Every top-level package whose code runs the tests or carries their
#: reports, and the one distribution allowed to install it.
PINNED_PACKAGES: Mapping[str, str] = {
    "_pytest": "pytest",
    "pytest": "pytest",
    "pluggy": "pluggy",
    "xdist": "pytest-xdist",
    "execnet": "execnet",
    "pytest_cov": "pytest-cov",
    "pytest_asyncio": "pytest-asyncio",
    "anyio": "anyio",
}

#: The distributions a judged run cannot do without.
REQUIRED_DISTRIBUTIONS = ("pytest", "pluggy")

#: The variable that switches pytest's entry-point autoload off.
AUTOLOAD_OFF = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

#: The variables kew's report plugin reads — the same names
#: :mod:`kew.held_out_report_plugin` declares, which never imports kew.
REPORT_ENV = "KEW_HELD_OUT_REPORT"
SETTINGS_ENV = "KEW_HELD_OUT_SETTINGS"
CHECKOUT_ENV = "KEW_HELD_OUT_CHECKOUT"
TRUSTED_ENV = "KEW_HELD_OUT_TRUSTED"
NAME_PASSES_ENV = "KEW_HELD_OUT_NAME_PASSES"
LIST_COLLECTED_ENV = "KEW_HELD_OUT_LIST_COLLECTED"
EXPECTED_ENV = "KEW_HELD_OUT_EXPECTED"

#: The report plugin's module name: this, then random hex.
PLUGIN_PREFIX = "kew_held_out_"


def normal_name(name: str) -> str:
    """A distribution name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


class LockUnreadable(ValueError):
    """A ``uv.lock`` that does not parse, or does not list its packages."""


def locked_packages(text: str) -> dict[str, list[dict[str, Any]]]:
    """Each ``[[package]]`` of a ``uv.lock``, by normalized name.

    A name listed twice (a fork per platform) keeps every entry, so neither
    stands in for the other when two locks are compared.
    """
    try:
        packages = tomllib.loads(text).get("package", [])
    except tomllib.TOMLDecodeError as exc:
        raise LockUnreadable(f"does not parse: {exc}") from exc
    if not isinstance(packages, list):
        raise LockUnreadable("does not list its packages")
    found: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        if isinstance(package, dict) and isinstance(package.get("name"), str):
            found.setdefault(normal_name(package["name"]), []).append(package)
    return found


def _identity(entries: list[dict[str, Any]]) -> list[tuple[str, str, tuple[str, ...]]]:
    """What a pin installs: its version, its source and every artifact hash."""
    identity = []
    for entry in entries:
        artifacts = [entry.get("sdist") or {}, *(entry.get("wheels") or [])]
        hashes = tuple(
            sorted(str(a.get("hash", "")) for a in artifacts if isinstance(a, dict))
        )
        source = json.dumps(entry.get("source"), sort_keys=True)
        identity.append((str(entry.get("version", "")), source, hashes))
    return sorted(identity)


@dataclass(frozen=True)
class Pins:
    """What a judged run may load: versions from the base's lock, and why not."""

    #: Each pinned distribution the base's lock carries, and its version;
    #: ``None`` for a project with no lock (:func:`unlocked`).
    versions: tuple[tuple[str, str], ...] | None

    #: Why the run is refused whatever it reports, if it is.
    refused: tuple[str, ...] = ()

    @property
    def plugins(self) -> tuple[PinnedPlugin, ...]:
        """The pinned plugins the base's lock installs, loaded by ``-p``."""
        locked = dict(self.versions or ())
        return tuple(p for p in PINNED_PYTEST_PLUGINS if p.distribution in locked)

    def args(self) -> list[str]:
        """``-p <name>`` for each plugin the base's lock installs."""
        return [arg for plugin in self.plugins for arg in ("-p", plugin.name)]

    def settings(self) -> str:
        """The JSON the report plugin checks the run against."""
        return json.dumps(
            {
                "plugins": [[p.name, p.module, p.distribution] for p in self.plugins],
                "packages": dict(PINNED_PACKAGES),
                "versions": None if self.versions is None else dict(self.versions),
                "refused": list(self.refused),
            },
            sort_keys=True,
        )


#: No lock read yet: every run judged by these is refused.
NO_PINS = Pins(versions=(), refused=("kew read no pins for this run",))


def unlocked() -> Pins:
    """The pins of a project with no ``uv.lock``, which uv resolves afresh.

    Nothing pins a version, so none is checked and no ``-p`` plugin is loaded;
    every plugin and hook must still be an installed distribution's own file.
    """
    return Pins(versions=None)


def pins(base_lock: str | None, head_lock: str | None = None, *, lock: str) -> Pins:
    """The pins *base_lock* sets, refused where *head_lock* moves one.

    *lock* names the file in a refusal. No base lock, one kew cannot read, or
    one without a single pytest and pluggy pins nothing a run can be judged
    by, so the run is refused rather than allowed whatever it loads. A head
    lock of ``""`` is one the head deleted, and moves every pin.
    """
    if base_lock is None:
        return Pins(versions=(), refused=(f"the base has no {lock} to pin pytest by",))
    try:
        base = locked_packages(base_lock)
    except LockUnreadable as exc:
        return Pins(versions=(), refused=(f"the base's {lock} {exc}",))
    versions = {
        name: str(base[name][0].get("version", ""))
        for name in dict.fromkeys(PINNED_PACKAGES.values())
        if len(base.get(name, ())) == 1
    }
    refused = [
        f"the base's {lock} pins no single {name}"
        for name in REQUIRED_DISTRIBUTIONS
        if name not in versions
    ]
    if head_lock is not None:
        refused += _moved(base, head_lock, versions, lock)
    return Pins(versions=tuple(versions.items()), refused=tuple(refused))


def _moved(
    base: dict[str, list[dict[str, Any]]],
    head_lock: str,
    versions: Mapping[str, str],
    lock: str,
) -> list[str]:
    """Each pin the head's lock does not carry exactly as the base's does."""
    try:
        head = locked_packages(head_lock)
    except LockUnreadable as exc:
        return [f"the head's {lock} {exc}"]
    return [
        f"the head's {lock} moves {name} off the base's pin"
        for name in versions
        if _identity(head.get(name, [])) != _identity(base[name])
    ]
