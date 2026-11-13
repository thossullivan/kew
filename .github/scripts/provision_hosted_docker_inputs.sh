#!/usr/bin/env bash
# Provision the Docker inputs the serial tests read, on a GitHub-hosted runner.
#
# The private (self-hosted) runner keeps these as host state and never runs
# this script: its workflow steps are conditioned on
# runner.environment == 'github-hosted'. A hosted VM starts with an empty
# image store and no kew-allowlist network, so it builds them here, the same
# way release.yml's sandbox-smoke job does:
#
#   - the kew-allowlist network, as a plain bridge. It proves the container
#     and launch layers only; the egress restriction is host iptables and is
#     not verified on a hosted runner;
#   - the configured claude-code image, under its pinned tag, from
#     docker/agent.Dockerfile, after checking that the pin equals the digest of
#     the docker/ inputs.
#
# The kew-switchyard image is not built here: its pinned source input exists
# on the private runner alone, so the image tooling is opt-in
# (KEW_SWITCHYARD_IMAGE, docs/operations.md "Public repository runner rule").
set -euo pipefail

cd "$(dirname "$0")/../.."

if [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
    echo "provision_hosted_docker_inputs.sh: runs only on a GitHub-hosted runner; the private runner keeps these inputs as host state." >&2
    exit 1
fi

if ! docker network inspect kew-allowlist >/dev/null 2>&1; then
    docker network create --driver bridge kew-allowlist >/dev/null
fi

# An explicit exit, not `assert`: PYTHONOPTIMIZE strips assertions. Symlinks
# under docker/ are refused: the inputs digest hashes a link's target text,
# not the file it points at.
image="$(uv run --project kew python -c 'import sys; from pathlib import Path; import tomllib; from kew.sandbox import agent_image_inputs_digest; root = Path.cwd(); links = sorted(str(p.relative_to(root)) for p in (root / "docker").rglob("*") if p.is_symlink()); not links or sys.exit(f"symlinks under docker/ are not pinnable inputs: {links}"); configured = tomllib.loads((root / "kew.toml").read_text())["sandbox"]["images"]["claude-code"]; expected = f"kew-agent:docker-{agent_image_inputs_digest(root)[:12]}"; configured == expected or sys.exit(f"claude-code image pin mismatch: configured={configured!r}, expected={expected!r}"); print(configured)')"
# The image carries the full inputs digest as its dev.kew.agent-image-inputs
# label, from the module `kew sandbox build` labels with: a launch of the pin
# tag refuses an image without it.
label="$(uv run --project kew python -m kew.image_inputs "$PWD")"
docker build --label "$label" -f docker/agent.Dockerfile -t "$image" .
echo "provisioned kew-allowlist and $image"
