# =============================================================================
# kew-check
# The approved agent image plus kew's own Python and frontend dependencies,
# prebuilt, so kew's static checks and `[resolve]` checks run in a container
# with no network and a read-only checkout (#3281). Named by
# `[sandbox] check_image`; built by scripts/build-check-image.sh, which derives
# the tag from this file, the lock files and the base pin.
#
# Kept out of docker/: every file there feeds the agent images' digest pin
# (kew.sandbox.agent_image_inputs_digest) and ships in the package.
#
# kew itself is NOT in this image. The checks import it from the mounted
# checkout's kew/src (PYTHONPATH below), or they would test the image rather
# than the head under review.
# =============================================================================

# The `[sandbox.images] claude-code` pin from kew.toml. FROM it, so the check
# container keeps the agent image's user, entrypoint and containment contract.
ARG AGENT_IMAGE
FROM ${AGENT_IMAGE}

# kew/scripts/tool-pins.env's SEMGREP_VERSION and SETUPTOOLS_VERSION (the
# setuptools semgrep runs with, as AGENTS.md's semgrep command runs it), passed
# by build-check-image.sh, which names the image's tag after them. No default:
# a build without them stops rather than install a version no pin names.
ARG SEMGREP_VERSION
ARG SETUPTOOLS_VERSION

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Installs run as root into root-owned paths the agent user only reads.
USER root

ENV KEW_CHECK_HOME=/opt/kew-check
ENV UV_PYTHON_INSTALL_DIR=/opt/kew-check/python \
    UV_PROJECT_ENVIRONMENT=/opt/kew-check/venv \
    UV_TOOL_DIR=/opt/kew-check/tools \
    UV_TOOL_BIN_DIR=/opt/kew-check/bin \
    PLAYWRIGHT_BROWSERS_PATH=/opt/kew-check/ms-playwright

COPY kew/pyproject.toml kew/uv.lock /opt/kew-check/kew/
COPY frontend/package.json frontend/package-lock.json /opt/kew-check/frontend/

# --- kew's locked Python environment, without kew itself ---
# The build cache lives under /tmp and goes in the same layer; copy mode so the
# environment holds no links into it.
RUN : "${SEMGREP_VERSION:?build with scripts/build-check-image.sh}" \
        "${SETUPTOOLS_VERSION:?build with scripts/build-check-image.sh}" && \
    export UV_CACHE_DIR=/tmp/uv-build-cache UV_LINK_MODE=copy && \
    uv python install 3.13 && \
    cd /opt/kew-check/kew && \
    uv sync --frozen --no-install-project --all-extras --python 3.13 && \
    uv tool install --with "setuptools==${SETUPTOOLS_VERSION}" \
        "semgrep==${SEMGREP_VERSION}" && \
    /opt/kew-check/bin/semgrep --version && \
    rm -rf /tmp/uv-build-cache

# --- The frontend's node_modules and the ui-checks' Chromium ---
RUN export NPM_CONFIG_CACHE=/tmp/npm-build-cache && \
    cd /opt/kew-check/frontend && \
    npm ci --no-audit --no-fund && \
    node_modules/.bin/playwright-core install --with-deps chromium-headless-shell && \
    rm -rf /tmp/npm-build-cache /var/lib/apt/lists/*

# --- kew-check-frontend ---
# node_modules cannot be placed inside a read-only checkout, and svelte-kit
# writes its generated dir beside the sources. This runs its arguments in a
# /tmp copy of the checkout's frontend/ (plus the model catalog vite.config.ts
# reads) whose node_modules is the image's:
#   kew-check-frontend npm run check
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'src="${KEW_CHECK_CHECKOUT:-/work}"' \
    'tree=/tmp/kew-check/tree' \
    'rm -rf "$tree" && mkdir -p "$tree/kew/src/kew"' \
    'tar -C "$src" --exclude=frontend/node_modules --exclude=frontend/.svelte-kit -cf - frontend | tar -C "$tree" -xf -' \
    'cp "$src/kew/src/kew/models.json" "$tree/kew/src/kew/models.json"' \
    'ln -s /opt/kew-check/frontend/node_modules "$tree/frontend/node_modules"' \
    'cd "$tree/frontend"' \
    'exec "$@"' \
    > /opt/kew-check/bin/kew-check-frontend && \
    chmod 0755 /opt/kew-check/bin/kew-check-frontend

# --- Offline, and nothing written into the checkout ---
# `uv run` uses the prebuilt environment as it is: no sync (which would install
# kew into it), no lock, no network, no `.venv` in the checkout. Every tool
# cache goes under /tmp, the one writable path a `--read-only` run has.
ENV PATH=/opt/kew-check/bin:${PATH} \
    UV_NO_SYNC=1 \
    UV_FROZEN=1 \
    UV_OFFLINE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/kew-check/uv \
    PYTHONPATH=/work/kew/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    RUFF_CACHE_DIR=/tmp/kew-check/ruff \
    MYPY_CACHE_DIR=/tmp/kew-check/mypy \
    NPM_CONFIG_CACHE=/tmp/kew-check/npm \
    XDG_CACHE_HOME=/tmp/kew-check/cache \
    SEMGREP_SETTINGS_FILE=/tmp/kew-check/semgrep-settings.yml \
    SEMGREP_SEND_METRICS=off

USER agent
WORKDIR /work
