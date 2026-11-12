"""Static contract tests for the OpenCode agent image catalog workaround."""

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[2]
_DOCKERFILE = _REPO_ROOT / "docker" / "agent-opencode.Dockerfile"
_ENTRYPOINT = _REPO_ROOT / "docker" / "agent-opencode-entrypoint.sh"
_CATALOG = _REPO_ROOT / "docker" / "opencode.json"
_MODEL = "deepseek/deepseek-v4-flash-0731"


def test_opencode_image_declares_the_post_pin_openrouter_model() -> None:
    # OpenCode accepts JSONC and ruff formats JSON assets with trailing commas.
    source = re.sub(
        r",\s*([}\]])",
        r"\1",
        _CATALOG.read_text(encoding="utf-8"),
    )
    payload = json.loads(source)

    model = payload["provider"]["openrouter"]["models"][_MODEL]
    assert model["name"] == "DeepSeek V4 Flash 0731"


def test_opencode_image_installs_config_into_the_runtime_home() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")

    # --chmod pins the destination mode; without it COPY carries the checkout
    # umask's read/write bits into the layer (#1793 round-4 review).
    assert (
        "COPY --chown=agent:agent --chmod=0644 docker/opencode.json "
        "/usr/local/share/kew/opencode.json"
    ) in dockerfile
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")
    assert "cp /usr/local/share/kew/opencode.json" in entrypoint


def test_opencode_entrypoint_probes_catalog_before_agent_launch() -> None:
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "command -v opencode" in entrypoint
    assert '"$AGENT_PROGRAM" models openrouter --pure' in entrypoint
    # Whole-line match: a substring probe would accept a truncated model id.
    assert 'grep -Fxq -- "$OPENCODE_MODEL"' in entrypoint
    assert "OpenCode catalog gap" in entrypoint
    assert 'exec "$AGENT_PROGRAM" "$@"' in entrypoint
