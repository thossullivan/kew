# =============================================================================
# kew-agent-opencode
# Lean, non-root, credential-free image for dispatched OpenCode workers
# (the OpenRouter lane: `[routing] simple` and any opencode-routed node).
#
# Implements docs/sandbox-entrypoint-contract.md — the first non-reference
# image built against the contract (#779 operator wave). The OpenRouter key is
# injected at `docker run` time by name only (`-e OPENROUTER_API_KEY`), per
# the runner's `required_credentials` declaration (#1146).
# Never bake credentials into this image.
# =============================================================================

# Pinned by digest for reproducibility and supply-chain integrity.
# debian:trixie-slim, manifest verified 2026-09-10.
# Trixie resolves the observed nftables 1.0.6 host-ruleset crash.
# To advance it: pull debian:trixie-slim, read the RepoDigest off the
# pulled image, paste it here in BOTH agent Dockerfiles, and let the CI
# agent-image-scan job re-scan what the new base ships.
# Same base as agent.Dockerfile, advanced with it so the two never diverge.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS base

# Every RUN below runs under bash with pipefail (#2902). Under the default
# `/bin/sh -c`, a `curl ... | bash -` whose download fails pipes an empty
# script into bash, which exits 0, so the build carries on: an unreachable
# nodesource once let apt install Debian's own Node 20 in place of Node 22.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- System packages ---
# Same set as the reference image: procps (#762) for fleet/process checks in
# repo test suites, jq (#797) for the security-guard hook payload.
#
# `apt-get upgrade` runs first so security fixes Debian publishes AFTER the
# pinned base digest still land in the image. Without it the base layer's
# packages are frozen at the digest's date, and the scan gate fails on every
# fix Debian ships in between (libexpat1 CVE-2026-56408, 2026-09-01). The
# digest pin still governs what the build starts from; the gate's iidfile
# scan governs what it produces.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    procps \
    && rm -rf /var/lib/apt/lists/*

# --- Node.js 22 (for the OpenCode CLI and frontend acceptance gates) ---
#
# nodesource's apt repository is configured here by hand, never by its
# `setup_22.x` script: that is a program whose bytes are whatever the URL
# serves on the day, piped into a root shell (#2428). The repository's signing
# key is checked against its SHA-256 before apt trusts it -- the key is
# 6F71F525282841EEDAF851B42F59B5F99B1BE0B4, "NSolid <nsolid-gpg@nodesource.com>"
# -- and apt then installs one exact build, so every rebuild gets the same Node
# rather than whatever minor nodesource published last (#3160). To advance it:
# read the `nodejs` Version from
# https://deb.nodesource.com/node_22.x/dists/nodistro/main/binary-amd64/Packages
# and change it, and the version check below, in ALL THREE agent Dockerfiles.
#
# The install must yield that Node with its npm. If the nodesource repository
# never got configured, apt has no such version to install, and a Debian
# `nodejs` -- a different major with no npm -- fails the checks below (#2902).
RUN curl -fsSL -o /tmp/nodesource.asc \
        https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key && \
    if ! printf '%s  %s\n' \
        b42e0321dabdc24e892115da705cf061167eac12a317f23d329862d0aa0a271d \
        /tmp/nodesource.asc | sha256sum --check --status; then \
        echo 'the nodesource signing key does not match its pinned SHA-256' >&2; exit 1; \
    fi && \
    install -D -m 0644 /tmp/nodesource.asc /etc/apt/keyrings/nodesource.asc && \
    rm -f /tmp/nodesource.asc && \
    printf '%s\n' \
        'deb [signed-by=/etc/apt/keyrings/nodesource.asc] https://deb.nodesource.com/node_22.x nodistro main' \
        | tee /etc/apt/sources.list.d/nodesource.list >/dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs=22.23.4-1nodesource1 && \
    rm -rf /var/lib/apt/lists/* && \
    node_version="$(node --version)" && \
    if [ "$node_version" != v22.23.4 ]; then \
        echo "expected nodesource Node v22.23.4, got $node_version" >&2; exit 1; \
    fi && \
    if ! command -v npm >/dev/null; then echo 'npm missing from the nodejs install' >&2; exit 1; fi

# The overlay verifier (see the npm step below). Copied before it runs so the
# build fails at the check rather than at a missing file.
COPY --chmod=0755 docker/verify-npm-overlay.js /usr/local/bin/kew-verify-npm-overlay

# --- npm bundled-dependency CVEs ---
# Every fixable CRITICAL/HIGH Trivy reports for this image lives under
# /usr/lib/node_modules/npm/node_modules -- npm's own vendored dependencies,
# shipped by the nodesource `nodejs` package. Two steps, because one is not
# enough:
#
#  1. Upgrading npm clears most of them, including the CRITICAL node-tar gzip
#     bomb (CVE-2026-59873). PINNED, not @latest: this is a supply-chain node,
#     and `npm@latest` would let an unreviewed npm into the containment
#     boundary on any rebuild, with no repository change to review and no CVE
#     for Trivy to catch (round-1 review, Major 5). Bump deliberately.
#  2. npm 12.0.2 still VENDORS brace-expansion 5.0.7, ip-address 10.2.0,
#     tar 7.5.19 and undici 6.27.0, each with a published fix npm has not picked
#     up. npm cannot install into its own prefix (it tries to resolve npm's
#     whole package.json and 404s on @npmcli/docs), so the patched packages are
#     staged in a scratch prefix and copied over the vendored copies.
#     On 2026-10-12, brace-expansion was bumped for CVE-2026-102276 and
#     CVE-2026-102278, and undici was added for CVE-2026-19534.
#     On 2026-10-24, undici moved to 6.29.1 for CVE-2026-109869.
#
# The overlay copies four top-level directories and RELIES on npm's sibling
# packages satisfying their dependency ranges. Today they do -- npm vendors
# balanced-match 4.0.4 for brace-expansion's ^4.0.2, ip-address 10.3.1 has no
# dependencies at all, and tar's minipass/minizlib/chownr/yallist are already
# current. That is luck, not design, so the build ASSERTS it: the check below
# runs every replaced package through Node's resolver and fails if a dependency
# is missing. A future npm that reshapes its bundle breaks the build here rather
# than a dispatch later.
#
# The `agent-image-scan` CI job proves the CVEs stay gone. It cannot tell you
# when step 2 becomes unnecessary -- when a future npm vendors these itself,
# this step turns into a no-op that still passes. Re-check when bumping npm.
RUN npm install -g npm@12.0.2 && \
    npm install --prefix /tmp/npmfix --no-save --no-audit --no-fund --silent \
        brace-expansion@5.0.11 ip-address@10.3.1 tar@7.5.21 undici@6.29.1 && \
    for pkg in brace-expansion ip-address tar undici; do \
        rm -rf "/usr/lib/node_modules/npm/node_modules/$pkg" && \
        cp -R "/tmp/npmfix/node_modules/$pkg" "/usr/lib/node_modules/npm/node_modules/$pkg"; \
    done && \
    rm -rf /tmp/npmfix && \
    node /usr/local/bin/kew-verify-npm-overlay && \
    cd /tmp && npm init -y >/dev/null && \
    npm install --no-audit --no-fund --silent is-number@7.0.0 && \
    npm pack is-number@7.0.0 >/dev/null && \
    rm -rf /tmp/package.json /tmp/node_modules /tmp/*.tgz && \
    npm cache clean --force

# --- OpenCode CLI ---
# Pinned to the version proven on the host through the O1/O2 parity work and
# the governance-hardening drive. Bump deliberately, with a smoke + one
# end-to-end contained dispatch.
# npm 12 (pinned above) does not run the package's postinstall on a global
# install, and opencode-ai's postinstall is what places the platform binary at
# bin/opencode.exe. Without it every `opencode` invocation exits 1 with "run the
# postinstall script manually" and the image cannot dispatch at all (found
# 2026-09-03 on the first OpenRouter dogfood after #1793). Run it explicitly and
# prove the CLI answers before the layer is sealed.
#
# The `test -x` names the same binary docker/openshell/policy-opencode.yaml
# governs. That path, not the npm symlink and not node, is the process that
# talks to OpenRouter, so a policy that misnames it governs nothing and every
# contained dispatch fails closed on its first model call. Asserting it here
# turns the policy's path from a comment into a build-gated fact.
RUN npm install -g opencode-ai@1.18.5 && \
    cd /usr/lib/node_modules/opencode-ai && node postinstall.mjs && \
    test -x /usr/lib/node_modules/opencode-ai/bin/opencode.exe && \
    opencode --version

# --- uv (Python package manager) ---
# Same rationale as the reference image (#762): preinstalled on PATH so
# Python-repo agents don't re-download it into /work every dispatch.
#
# From the release archive, checked against its SHA-256 before anything is
# extracted -- never the astral.sh installer script piped into a shell (#2428).
# The version and digests are the ones kew/src/kew/hosts/provision/trivy.pin
# names for the verify host (UV_VERSION, UV_SHA256_*), so the image and the
# host run the same uv; tests/test_dockerfile_pipe_to_shell.py fails if they
# drift. Advance them together.
RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        amd64) target=x86_64-unknown-linux-gnu; \
            sha=f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f ;; \
        arm64) target=aarch64-unknown-linux-gnu; \
            sha=4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403 ;; \
        *) echo "no uv build is pinned for $arch" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/uv.tar.gz \
        "https://github.com/astral-sh/uv/releases/download/0.13.5/uv-$target.tar.gz" && \
    if ! printf '%s  %s\n' "$sha" /tmp/uv.tar.gz | sha256sum --check --status; then \
        echo 'the uv archive does not match its pinned SHA-256' >&2; exit 1; \
    fi && \
    tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 --no-same-owner \
        "uv-$target/uv" "uv-$target/uvx" && \
    chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx && \
    rm -f /tmp/uv.tar.gz && \
    /usr/local/bin/uv --version

# --- Non-root user + agent-owned workdir ---
# uid 1001 matches the AGENT_HOME tmpfs pin in kew.sandbox (_AGENT_UID).
RUN useradd -m -s /bin/bash -u 1001 agent \
    && mkdir -p /usr/local/share/kew \
    && mkdir -p /work \
    && chown agent:agent /work

# OpenCode's model catalog is extended without changing the pinned CLI. The
# runtime HOME is a fresh tmpfs, so the entrypoint copies this immutable image
# asset into the per-run config directory before probing or launching.
# --chmod pins the destination mode. Without it COPY carries the source file's
# read/write bits, which are the checkout umask's, so two builds of one pin
# could differ in layer metadata (round-4 review).
COPY --chown=agent:agent --chmod=0644 docker/opencode.json /usr/local/share/kew/opencode.json

# --- Prompt-handoff entrypoint ---
# Contract element 2: prompt file -> agent stdin, never argv. See
# docker/agent-opencode-entrypoint.sh.
COPY --chown=agent:agent --chmod=0755 docker/agent-opencode-entrypoint.sh /usr/local/bin/kew-agent-entrypoint

# --- OpenShell launcher ---
# Under `[sandbox] runtime = "openshell"` the ENTRYPOINT above never runs:
# `openshell sandbox exec` replaces it, so the host invokes this script instead
# and it does the same pre-runner work. Installed in BOTH agent images so one
# launcher serves both runners. See docker/kew-openshell-launch.
COPY --chown=agent:agent --chmod=0755 docker/kew-openshell-launch /usr/local/bin/kew-openshell-launch

# --- Publication producer (W1.1 D3) ---
# Every image a contained dispatch can run needs this, or publication silently
# depends on which runner was chosen. Same path and same contract as
# docker/agent.Dockerfile — element 6 of docs/sandbox-entrypoint-contract.md.
COPY --chown=agent:agent --chmod=0755 docker/extract-artifact.sh /usr/local/bin/kew-extract-artifact

USER agent
WORKDIR /work

ENTRYPOINT ["/usr/local/bin/kew-agent-entrypoint"]
