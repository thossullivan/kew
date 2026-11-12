"""`kew verify nightly install`: the nightly full verify as a launchd agent (#3195).

Follows :mod:`kew.drive_agent`: a per-user LaunchAgent with no credential in
it, a minimal ``PATH``, the project and the ``KEW_*`` config variables pinned,
and an optional ``--env-file`` the agent sources before it starts. Where the
drive's agent runs at login and restarts on a crash, this one runs
``kew verify nightly`` once a day at 01:30 local time (:data:`NIGHTLY_HOUR`,
:data:`NIGHTLY_MINUTE`) and never restarts itself: a nightly that did not run
reads stale (:data:`kew.main_health.STALE_AFTER`), not retried.

Install and uninstall hold the same agent lock beside the plist the drive's
agent uses, and only replace or remove a plist carrying :data:`NIGHTLY_MARK`.
"""

from __future__ import annotations

import hashlib
import logging
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kew.drive_agent import (
    _CREDENTIAL_SHAPES,
    _SHELL_PREFIX,
    AgentRefused,
    AgentSpec,
    _bootout,
    _bootstrap,
    _write_new,
    agent_lock,
    agent_plist,
)

logger = logging.getLogger(__name__)

LABEL_PREFIX = "dev.kew.verify-nightly."

#: kew's mark on the plists it writes for the nightly.
NIGHTLY_MARK = "KEW_NIGHTLY_AGENT"

#: When launchd runs the nightly, local time.
NIGHTLY_HOUR = 1
NIGHTLY_MINUTE = 30

#: The nightly's log, beside the ledger.
NIGHTLY_LOG = "verify-nightly.log"


def nightly_label(project: Path) -> str:
    """One label per project directory."""
    name = re.sub(r"[^A-Za-z0-9-]+", "-", project.name).strip("-") or "project"
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:10]
    return f"{LABEL_PREFIX}{name}.{digest}"


@dataclass(frozen=True)
class NightlySpec(AgentSpec):
    """What the agent runs: ``binary verify nightly`` in ``project``."""

    @property
    def label(self) -> str:
        return nightly_label(self.project)

    def program(self) -> list[str]:
        argv = [str(self.binary), "verify", "nightly"]
        if self.env_file is None:
            return argv
        return [*_SHELL_PREFIX, str(self.env_file), *self.pins(), "--", *argv]

    def environment_variables(self) -> dict[str, str]:
        env = super().environment_variables()
        env.pop("KEW_DRIVE_AGENT", None)
        env[NIGHTLY_MARK] = "1"
        return env

    def render(self) -> bytes:
        data = self._plist()
        for form, shape in _CREDENTIAL_SHAPES.items():
            if shape.search(data):
                raise AgentRefused(
                    f"the nightly's plist would hold {form}; keep credentials in "
                    "--env-file, not in KEW_* settings, --kew-binary or paths"
                )
        return data

    def _plist(self) -> bytes:
        definition: dict[str, Any] = {
            "Label": self.label,
            "ProgramArguments": self.program(),
            "WorkingDirectory": str(self.project),
            "EnvironmentVariables": self.environment_variables(),
            "StartCalendarInterval": {
                "Hour": NIGHTLY_HOUR,
                "Minute": NIGHTLY_MINUTE,
            },
            "ProcessType": "Background",
            "StandardOutPath": str(self.log),
            "StandardErrorPath": str(self.log),
        }
        return plistlib.dumps(definition)


def _ours(plist: Path) -> bool:
    """Whether *plist* carries :data:`NIGHTLY_MARK`; an unreadable one is not
    kew's, so it is refused rather than replaced, and says why."""
    try:
        data = plistlib.loads(plist.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        logger.warning("%s is unreadable, so it is not kew's nightly: %s", plist, exc)
        return False
    env = data.get("EnvironmentVariables")
    return isinstance(env, dict) and env.get(NIGHTLY_MARK) == "1"


def _replaceable(plist: Path) -> bool:
    """Whether a plist is at *plist* to boot out first; a foreign one refuses."""
    if plist.is_symlink():
        raise AgentRefused(f"{plist} is a symlink; kew will not write through it")
    if not plist.exists():
        return False
    if not _ours(plist):
        raise AgentRefused(
            f"{plist} is not the nightly agent kew writes; move it aside first"
        )
    return True


def install(spec: NightlySpec, home: Path | None = None) -> Path:
    """Write and load the nightly's plist; return its path."""
    data = spec.render()
    plist = agent_plist(spec.label, home)
    with agent_lock(spec.label, home, create=True):
        if _replaceable(plist):
            _bootout(spec.label)
        _write_new(plist, data, 0o644)
        failure = _bootstrap(plist)
    if failure is not None:
        raise AgentRefused(failure)
    return plist


def uninstall(project: Path, home: Path | None = None) -> Path | None:
    """Unload and remove the nightly's plist; ``None`` when none is installed."""
    label = nightly_label(project)
    plist = agent_plist(label, home)
    with agent_lock(label, home):
        if not _replaceable(plist):
            return None
        _bootout(label)
        plist.unlink()
    return plist
