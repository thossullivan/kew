"""Every workflow that installs Node runs the one Node the locks accept.

``frontend/.npmrc`` sets ``engine-strict``, so ``npm ci`` refuses a Node that a
locked dependency's ``engines`` field rules out. CI pinned Node 20 while the
frontend lock needed 22.19 or newer, and a bare major ("24") can still resolve
below a range such as ``^24.15.0`` on a stale runner cache. So the repository
pins one minor in ``.nvmrc``, every ``actions/setup-node`` step reads that
file, and the pin's whole minor (its lowest and a late patch) must satisfy
every locked ``engines.node`` range of the frontend and the site.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
NVMRC = REPO_ROOT / ".nvmrc"
LOCKS = (REPO_ROOT / "frontend/package-lock.json", REPO_ROOT / "www/package-lock.json")

Version = tuple[int, int, int]
_COMPARATOR = re.compile(r"^(\^|~|>=|<=|>|<|=)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


def _bounds(token: str) -> tuple[Version, Version | None, bool]:
    """(lowest accepted, first refused or None, lowest inclusive) of one
    comparator. An unknown spelling fails, so a new one is read, not passed."""
    if token in {"*", "x", ""}:
        return (0, 0, 0), None, True
    match = _COMPARATOR.match(token)
    assert match, f"unread engines comparator {token!r}"
    op, major, minor, patch = match.groups()
    x, y, z = int(major), int(minor or 0), int(patch or 0)
    if op == "^":
        upper: Version = (x + 1, 0, 0) if x else (0, y + 1, 0)
        return (x, y, z), upper, True
    if op == "~":
        return (x, y, z), (x, y + 1, 0), True
    if op == ">=":
        return (x, y, z), None, True
    if op == ">":
        return (x, y, z), None, False
    if op in {"<", "<="}:
        top = (x, y, z + 1) if op == "<=" else (x, y, z)
        return (0, 0, 0), top, True
    if minor is None:
        return (x, 0, 0), (x + 1, 0, 0), True
    if patch is None:
        return (x, y, 0), (x, y + 1, 0), True
    return (x, y, z), (x, y, z + 1), True


def satisfies(version: Version, node_range: str) -> bool:
    """Whether ``version`` meets an npm ``engines.node`` range."""
    for alternative in node_range.split("||"):
        tokens = re.sub(r"(>=|<=|>|<|=)\s+", r"\1", alternative).split()
        ok = True
        for token in tokens or [""]:
            low, high, inclusive = _bounds(token)
            above = version >= low if inclusive else version > low
            ok = ok and above and (high is None or version < high)
        if ok:
            return True
    return False


def _pin() -> str:
    return NVMRC.read_text().strip()


def _pinned_versions() -> list[Version]:
    """The lowest and a late patch of the pinned minor."""
    match = re.fullmatch(r"(\d+)\.(\d+)", _pin())
    assert match, f".nvmrc must name one minor, X.Y: {_pin()!r}"
    major, minor = int(match.group(1)), int(match.group(2))
    return [(major, minor, 0), (major, minor, 99)]


def _setup_node_steps() -> list[tuple[str, dict]]:
    steps = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        data = yaml.safe_load(path.read_text())
        for job_name, job in (data.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/setup-node@"):
                    steps.append((f"{path.name}/{job_name}", step.get("with") or {}))
    return steps


def _locked_ranges() -> dict[str, str]:
    ranges = {}
    for lock_path in LOCKS:
        lock = json.loads(lock_path.read_text())
        for name, package in lock["packages"].items():
            engines = package.get("engines")
            node = engines.get("node") if isinstance(engines, dict) else None
            if isinstance(node, str):
                ranges[f"{lock_path.parent.name}:{name}"] = node
    return ranges


def _npmrc() -> str:
    return (REPO_ROOT / "frontend/.npmrc").read_text()


@pytest.mark.policy
def test_every_setup_node_step_reads_the_one_pin() -> None:
    steps = _setup_node_steps()
    jobs = {where for where, _ in steps}
    assert {"ci.yml/frontend", "ci.yml/www", "release.yml/release"} <= jobs
    for where, with_ in steps:
        assert with_.get("node-version-file") == ".nvmrc", where
        assert "node-version" not in with_, where


def test_the_pinned_minor_meets_every_locked_engine_range() -> None:
    assert "engine-strict=true" in _npmrc()
    ranges = _locked_ranges()
    # The range that refused Node 20 and the one a bare "24" could miss.
    assert ranges["frontend:node_modules/undici"] == ">=22.19.0"
    assert "^24.15.0" in ranges["frontend:node_modules/jsdom"]
    refused = [
        f"{name} ({node_range}) refuses {'.'.join(map(str, version))}"
        for version in _pinned_versions()
        for name, node_range in ranges.items()
        if not satisfies(version, node_range)
    ]
    assert refused == []


@pytest.mark.parametrize(
    ("version", "node_range", "expected"),
    [
        ((20, 19, 0), ">=22.19.0", False),
        ((24, 14, 9), "^22.22.2 || ^24.15.0 || >=26.0.0", False),
        ((24, 15, 0), "^22.22.2 || ^24.15.0 || >=26.0.0", True),
        ((25, 0, 0), "^22.22.2 || ^24.15.0 || >=26.0.0", False),
        ((24, 15, 0), "20 || >=22", True),
        ((21, 0, 0), "20 || >=22", False),
        ((24, 15, 0), ">=v12.22.7", True),
        ((24, 15, 0), "^20.19 || ^22.12 || >=24", True),
        ((24, 15, 0), ">= 18 < 24", False),
        ((22, 1, 0), "~22.0.3", False),
    ],
)
def test_the_range_reader_follows_npm(
    version: Version, node_range: str, expected: bool
) -> None:
    assert satisfies(version, node_range) is expected


def test_an_unknown_range_spelling_is_not_passed() -> None:
    with pytest.raises(AssertionError, match="unread engines comparator"):
        satisfies((24, 15, 0), "node@lts")
