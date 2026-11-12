"""Security hook generation and management."""

import json
import stat
from pathlib import Path
from typing import Any

from kew.config import DispatchConfig

MANAGED_HEADER = "# managed-by: kew"


def _shell_escape(s: str) -> str:
    """Escape a string for safe interpolation inside a bash double-quoted string."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def is_user_managed(hook_path: Path) -> bool:
    """Check if a hook file exists and is user-managed (not ours)."""
    if not hook_path.exists():
        return False
    content = hook_path.read_text()
    return MANAGED_HEADER not in content


def render_security_hook(config: DispatchConfig) -> str:
    """Render security-guard.sh content: template + config extras injected."""
    template_path = Path(__file__).parent / "_templates" / "security-guard.sh"
    template = template_path.read_text()

    # Inject custom patterns (escape special chars for bash double-quoted strings)
    extra_prefix = "\n".join(
        f'    "{_shell_escape(p)}"' for p in config.security.blocked_prefix_patterns
    )
    extra_exact = "\n".join(
        f'    "{_shell_escape(p)}"' for p in config.security.blocked_exact_patterns
    )

    content = template.replace("# EXTRA_PREFIX_PATTERNS", extra_prefix)
    return content.replace("# EXTRA_EXACT_PATTERNS", extra_exact)


def generate_security_hook(hook_path: Path, config: DispatchConfig) -> None:
    """Generate security-guard.sh from template + config patterns."""
    if is_user_managed(hook_path):
        return  # Don't overwrite user customizations

    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(render_security_hook(config))
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)


def _ensure_hook_for_matcher(
    entries: list[dict[str, Any]], matcher: str, hook: dict[str, str]
) -> None:
    """Register *hook* under *matcher* and replace its legacy string form."""
    entry = next(
        (e for e in entries if isinstance(e, dict) and e.get("matcher") == matcher),
        None,
    )
    if entry is not None:
        hooks_list = entry.get("hooks", [])
        if not isinstance(hooks_list, list):
            hooks_list = []
        legacy_hook = hook["command"]
        hooks_list = [
            registered
            for registered in hooks_list
            if registered != legacy_hook and registered != hook
        ]
        hooks_list.append(hook)
        entry["hooks"] = hooks_list
    else:
        entries.append({"matcher": matcher, "hooks": [hook]})


def wire_hook_into_settings(settings_path: Path) -> None:
    """Add the security hook to .claude/settings.json."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if settings_path.exists():
        data = json.loads(settings_path.read_text())

    if "hooks" not in data or not isinstance(data["hooks"], dict):
        data["hooks"] = {}
    existing = data["hooks"].get("PreToolUse", [])
    if not isinstance(existing, list):
        existing = []

    guard_hook = {
        "type": "command",
        "command": ".claude/hooks/security-guard.sh",
    }
    for matcher in ("Bash", "Write", "Edit"):
        _ensure_hook_for_matcher(existing, matcher, guard_hook)
    data["hooks"]["PreToolUse"] = existing

    settings_path.write_text(json.dumps(data, indent=2) + "\n")
