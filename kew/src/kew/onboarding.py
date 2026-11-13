"""What ``kew init`` detects about a repository, and the file it writes (#2502).

Detection only reads: the GitHub repository (``gh repo view``), the container
runtime on ``PATH``, the runner CLIs on ``PATH``, and the CI and format commands
a repository already declares in its ``justfile``, ``Makefile``,
``package.json`` or ``pyproject.toml``. Nothing here writes a file or reaches a
network beyond that one ``gh`` read; the CLI decides what to do with the answer.

The file written is the smallest one that says what was chosen: a value equal
to the shipped default is left out, so a later change to that default reaches
the project instead of being pinned by a file nobody meant to pin it.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kew import work_dirs
from kew.config import DispatchConfig, load_config
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.subprocess_utils import RUNNER_REQUIREMENTS, register_tools, run

#: Runner preference when ``--runner`` is not given: the first one installed.
RUNNER_PREFERENCE: tuple[str, ...] = tuple(RUNNER_REQUIREMENTS)

#: Container runtimes init will pick on its own, in preference order. OpenShell
#: is a gateway, not a binary on PATH; it is chosen with ``--runtime`` only.
_DETECTABLE_RUNTIMES = ("docker", "podman")

#: Recipe/target/script names, best first, for each command kew runs.
_CI_NAMES = ("ci", "test")
_FORMAT_NAMES = ("fmt", "format")

#: ``name:`` at column 0, not ``name :=`` (an assignment). A just recipe may
#: take parameters (``test *ARGS:``) and be quiet (``@test:``).
_JUST_RECIPE = re.compile(
    r"^@?(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?:\s+[^:=\n]*)?:(?!=)", re.MULTILINE
)
_MAKE_TARGET = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)

#: The ``(section, field)`` pairs init can set, in the order they are written.
INIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("project", "repo"),
    ("agent", "runner"),
    ("agent", "model"),
    ("commands", "ci"),
    ("commands", "format"),
    ("sandbox", "runtime"),
    ("budget", "per_issue_usd"),
)

RUNNER_FIELD = ("agent", "runner")

#: The ``kew init`` flag that sets each field; the replay command uses these.
INIT_FLAGS: dict[tuple[str, str], str] = {
    ("project", "repo"): "--repo",
    RUNNER_FIELD: "--runner",
    ("agent", "model"): "--model",
    ("commands", "ci"): "--ci",
    ("commands", "format"): "--format",
    ("sandbox", "runtime"): "--runtime",
    ("budget", "per_issue_usd"): "--budget-per-issue",
}

#: What an interactive init may ask, in asking order: the runtime before the
#: runner, since the posture table depends on it.
ASKED_FIELDS: tuple[tuple[str, str], ...] = (
    ("project", "repo"),
    ("commands", "ci"),
    ("commands", "format"),
    ("sandbox", "runtime"),
    RUNNER_FIELD,
)

RUNNER_CREDENTIAL_CHECK = "runner-credential"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    """What the repository and this machine already say."""

    repo: str | None
    runtime: str | None
    runners: tuple[str, ...]
    ci: str | None
    format: str | None


def detect_repo(root: Path) -> str | None:
    """``owner/name`` of the GitHub repository at ``root``, or ``None``."""
    register_tools()
    try:
        result = run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            check=False,
            cwd=root,
            timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _log.warning("kew init: repository not detected; gh did not run: %s", exc)
        return None
    name = result.stdout.strip() if result.returncode == 0 else ""
    return name or None


def detect_runtime() -> str | None:
    """The first container runtime on ``PATH``, by name."""
    from kew.config import SandboxConfig
    from kew.sandbox import detect_runtime_cached

    for name in _DETECTABLE_RUNTIMES:
        if detect_runtime_cached(SandboxConfig(runtime=name)) is not None:
            return name
    return None


def detect_runners() -> tuple[str, ...]:
    """Every runner whose CLI is on ``PATH``, in preference order."""
    return tuple(
        name
        for name in RUNNER_PREFERENCE
        if shutil.which(RUNNER_REQUIREMENTS[name][0]) is not None
    )


def _read(path: Path) -> str | None:
    """``path``'s text; ``None`` when absent, or unreadable (and said so)."""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("kew init: ignoring unreadable %s: %s", path, exc)
        return None


def _first(names: tuple[str, ...], present: set[str]) -> str | None:
    return next((name for name in names if name in present), None)


def _from_recipes(
    text: str | None, pattern: re.Pattern[str], tool: str
) -> tuple[str | None, str | None]:
    if text is None:
        return None, None
    present = {match.group("name") for match in pattern.finditer(text)}
    ci, fmt = _first(_CI_NAMES, present), _first(_FORMAT_NAMES, present)
    return (
        f"{tool} {ci}" if ci else None,
        f"{tool} {fmt}" if fmt else None,
    )


def _from_package_json(text: str | None) -> tuple[str | None, str | None]:
    if text is None:
        return None, None
    try:
        package = json.loads(text)
    except ValueError as exc:
        _log.warning("kew init: ignoring unparseable package.json: %s", exc)
        return None, None
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict):
        return None, None
    ci, fmt = _first(_CI_NAMES, set(scripts)), _first(_FORMAT_NAMES, set(scripts))
    return (
        ("npm test" if ci == "test" else f"npm run {ci}") if ci else None,
        f"npm run {fmt}" if fmt else None,
    )


def _from_pyproject(root: Path) -> tuple[str | None, str | None]:
    text = _read(root / "pyproject.toml")
    if text is None:
        return None, None
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as exc:
        _log.warning("kew init: ignoring unparseable pyproject.toml: %s", exc)
        return None, None
    prefix = "uv run " if (root / "uv.lock").is_file() else ""
    return (
        f"{prefix}pytest" if "pytest" in tool else None,
        f"{prefix}ruff format ." if "ruff" in tool else None,
    )


def detect_commands(root: Path) -> tuple[str | None, str | None]:
    """The CI and format commands ``root`` declares, first file that has one."""
    found = (
        _from_recipes(_read(root / "justfile"), _JUST_RECIPE, "just"),
        _from_recipes(_read(root / "Makefile"), _MAKE_TARGET, "make"),
        _from_package_json(_read(root / "package.json")),
        _from_pyproject(root),
    )
    return (
        next((ci for ci, _ in found if ci), None),
        next((fmt for _, fmt in found if fmt), None),
    )


def detect(root: Path) -> Detection:
    """Everything init reads before it chooses a value."""
    ci, fmt = detect_commands(root)
    return Detection(
        repo=detect_repo(root),
        runtime=detect_runtime(),
        runners=detect_runners(),
        ci=ci,
        format=fmt,
    )


def detected_values(detection: Detection) -> dict[tuple[str, str], object | None]:
    """The fields detection can answer, with its answer (``None`` when it had none)."""
    return {
        ("project", "repo"): detection.repo,
        RUNNER_FIELD: detection.runners[0] if detection.runners else None,
        ("commands", "ci"): detection.ci,
        ("commands", "format"): detection.format,
        ("sandbox", "runtime"): detection.runtime,
    }


def choose(
    detection: Detection, flags: Mapping[tuple[str, str], object | None]
) -> dict[tuple[str, str], object]:
    """Every value init will set: a flag wins, then detection; unset is absent."""
    detected = detected_values(detection)
    # An explicit empty answer ("no format command") is a choice, not a gap
    # for detection to fill: the replay command must reproduce it.
    candidates = {
        key: detected.get(key) if flags.get(key) is None else flags.get(key)
        for key in INIT_FIELDS
    }
    return {key: value for key, value in candidates.items() if value is not None}


def fields_to_ask(
    detection: Detection, flags: Mapping[tuple[str, str], object | None]
) -> tuple[tuple[str, str], ...]:
    """What an interactive init asks, in asking order (#2503).

    A field is asked only when neither a flag nor detection answered it. The
    runner is the exception: it is asked unless ``--runner`` was given, because
    it chooses the containment posture and what that costs. Model and budget
    are never guessed, so they are never asked either; they stay flags.
    """
    detected = detected_values(detection)
    return tuple(
        key
        for key in ASKED_FIELDS
        if flags.get(key) is None and (key == RUNNER_FIELD or detected[key] is None)
    )


def replay_command(values: Mapping[tuple[str, str], object]) -> str:
    """The ``kew init --yes`` command that writes the file ``values`` writes.

    Every chosen value is passed as a flag, defaults included, so the command
    does not depend on what the next machine happens to detect.
    """
    argv = ["kew", "init", "--yes"]
    for key in INIT_FIELDS:
        if key in values:
            argv.extend([INIT_FLAGS[key], str(values[key])])
    return shlex.join(argv)


def _posture(runner: str, runtime: str | None) -> tuple[str, str]:
    """``runner``'s containment posture under ``runtime``, and what it costs."""
    from kew.runners.registry import (
        runner_containment_posture,
        runner_uncontainable_reason,
    )
    from kew.sandbox import runner_credential_env_vars

    if runner_uncontainable_reason(runner) is not None:
        return "host only", "no boundary: it runs on this host with your access"
    if runner_containment_posture(runner, runtime=runtime) == "native":
        return "native", "nothing to build; it keeps its own CLI login"
    if runtime == OPENSHELL_RUNTIME:
        return "container", "an OpenShell sandbox; the gateway brokers its key"
    credentials = " or ".join(runner_credential_env_vars(runner)) or "a credential"
    return "container", f"an image to build, and {credentials} exported"


def posture_table(runtime: str | None, installed: tuple[str, ...]) -> list[str]:
    """One screen comparing every runner's posture and its cost (#1441)."""
    rows = [("runner", "installed", "posture", "what it costs")]
    for name in RUNNER_PREFERENCE:
        posture, cost = _posture(name, runtime)
        rows.append((name, "yes" if name in installed else "no", posture, cost))
    widths = [max(len(row[col]) for row in rows) for col in range(3)]
    return [
        "  ".join(
            [
                *(
                    cell.ljust(width)
                    for cell, width in zip(row[:3], widths, strict=True)
                ),
                row[3],
            ]
        )
        for row in rows
    ]


def minimal_values(
    values: dict[tuple[str, str], object],
) -> dict[tuple[str, str], object]:
    """``values`` without any field that equals its shipped default."""
    defaults = DispatchConfig()
    return {
        key: value
        for key, value in values.items()
        if getattr(getattr(defaults, key[0]), key[1]) != value
    }


def _toml_value(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"kew init cannot write {type(value).__name__} values")
    # A JSON string is a valid TOML basic string, escapes included.
    return json.dumps(value) if isinstance(value, str) else repr(value)


def render_toml(values: dict[tuple[str, str], object]) -> str:
    """``values`` as TOML, one table per section, in ``INIT_FIELDS`` order."""
    lines = ["# Written by `kew init`; only values that differ from kew's defaults."]
    section = None
    for key in INIT_FIELDS:
        if key not in values:
            continue
        if key[0] != section:
            section = key[0]
            lines.extend(["", f"[{section}]"])
        lines.append(f"{key[1]} = {_toml_value(values[key])}")
    return "\n".join(lines) + "\n"


def validate_toml(text: str) -> DispatchConfig:
    """Load ``text`` exactly as ``load_config`` would; raises ``ConfigError``.

    Read with an empty environment so a ``KEW_*`` override on this shell cannot
    make a bad file look valid, or a good one look bad.
    """
    with work_dirs.work_tempdir("kew-init-") as scratch:
        path = Path(scratch) / "kew.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path, env={})
