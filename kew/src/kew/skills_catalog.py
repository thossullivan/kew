"""Shipped-artifact catalog: manifest, drift states, install/update/diff.

Artifacts (Claude Code skills + the security-guard hook) ship as package
data and are provisioned into target repos' .claude/ by `kew skills`.
"""

import difflib
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kew.config import DispatchConfig
from kew.security import (
    MANAGED_HEADER,
    render_security_hook,
    wire_hook_into_settings,
)

_PKG_DIR = Path(__file__).parent
_SKILLS_DIR = _PKG_DIR / "_skills"
_MANIFEST_PATH = _SKILLS_DIR / "manifest.json"


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str  # "skill" | "hook"
    version: int
    source: str  # path relative to src/kew/
    target: str  # path relative to the target repo root
    sha256: str
    previous_sha256: tuple[str, ...] = ()


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> list[Artifact]:
    raw = json.loads(_MANIFEST_PATH.read_text())
    return [
        Artifact(
            name=entry["name"],
            kind=entry["kind"],
            version=entry["version"],
            source=entry["source"],
            target=entry["target"],
            sha256=entry["sha256"],
            previous_sha256=tuple(entry.get("previous_sha256", [])),
        )
        for entry in raw["artifacts"]
    ]


ArtifactState = Literal["not-installed", "current", "outdated", "locally-modified"]


def artifact_state(
    artifact: Artifact, repo_root: Path, config: DispatchConfig
) -> ArtifactState:
    """Classify the installed copy of *artifact* under *repo_root*.

    Skills compare by sha256 against the manifest's current + historical
    hashes. Hooks compare against the template rendered with *config* —
    extras live in kew.toml, so re-rendering re-applies the injected layer.
    """
    installed = repo_root / artifact.target
    if not installed.exists():
        return "not-installed"
    if artifact.kind == "hook":
        content = installed.read_text()
        if content == render_security_hook(config):
            return "current"
        if MANAGED_HEADER in content:
            return "outdated"
        return "locally-modified"
    digest = compute_sha256(installed)
    if digest == artifact.sha256:
        return "current"
    if digest in artifact.previous_sha256:
        return "outdated"
    return "locally-modified"


class LocallyModifiedError(Exception):
    """Raised when trying to update an artifact that has local modifications."""

    def __init__(self, artifact_name: str) -> None:
        self.artifact_name = artifact_name
        super().__init__(
            f"{artifact_name}: installed copy is locally modified; "
            "use --force to overwrite (kew skills diff shows the drift)"
        )


def _shipped_content(artifact: Artifact, config: DispatchConfig) -> str:
    """Get the content that should be shipped for this artifact."""
    if artifact.kind == "hook":
        return render_security_hook(config)
    return (_PKG_DIR / artifact.source).read_text()


def apply_artifact(
    artifact: Artifact,
    repo_root: Path,
    config: DispatchConfig,
    *,
    force: bool = False,
) -> str:
    """Install or update an artifact.

    Returns:
        "installed" if newly installed
        "updated" if re-applied over an outdated copy
        "unchanged" if already current
    """
    state = artifact_state(artifact, repo_root, config)
    if state == "current":
        if artifact.kind == "hook":
            # Re-wire settings even when the hook body is current (repairs
            # a deleted settings entry without touching the hook file).
            wire_hook_into_settings(repo_root / ".claude" / "settings.json")
        return "unchanged"
    if state == "locally-modified" and not force:
        raise LocallyModifiedError(artifact.name)

    dest = repo_root / artifact.target
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_shipped_content(artifact, config))
    if artifact.kind == "hook":
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
        wire_hook_into_settings(repo_root / ".claude" / "settings.json")
    return "installed" if state == "not-installed" else "updated"


def diff_artifact(artifact: Artifact, repo_root: Path, config: DispatchConfig) -> str:
    """Return unified diff of installed vs shipped content (empty if current)."""
    installed = repo_root / artifact.target
    if not installed.exists():
        return ""
    shipped = _shipped_content(artifact, config)
    have = installed.read_text()
    if have == shipped:
        return ""
    return "".join(
        difflib.unified_diff(
            have.splitlines(keepends=True),
            shipped.splitlines(keepends=True),
            fromfile=f"{artifact.target} (installed)",
            tofile=f"{artifact.target} (shipped v{artifact.version})",
        ),
    )
