# =============================================================================
# kew-agent
# Lean, non-root, credential-free image for dispatched coding-agent workers.
#
# The Anthropic API key is injected at `docker run` time via:
#   -e ANTHROPIC_API_KEY=<key>
# Never bake credentials into this image.
# =============================================================================

# Pinned by digest for reproducibility and supply-chain integrity.
# debian:trixie-slim, manifest verified 2026-09-10.
# Trixie resolves the observed nftables 1.0.6 host-ruleset crash.
# To advance it: pull debian:trixie-slim, read the RepoDigest off the
# pulled image, paste it here in BOTH agent Dockerfiles, and let the CI
# agent-image-scan job re-scan what the new base ships.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS base

# Every RUN below runs under bash with pipefail (#2902). Under the default
# `/bin/sh -c`, a `curl ... | bash -` whose download fails pipes an empty
# script into bash, which exits 0, so the build carries on: an unreachable
# nodesource once let apt install Debian's own Node 20 in place of Node 22.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- System packages ---
# procps (#762) provides `ps`, which the fleet's process checks and repo test
# suites (e.g. test_fleet) call. Contained Python-repo agents kept re-adding it.
# jq (#797) parses the security-guard.sh hook payload; without it the guard
# fails closed (refuses every tool call), so it must be in the image.
# nftables installs the worker-only Switchyard filter.
# iptables provides truthful legacy firewall readback for the SC-0 proof.
#
# `apt-get upgrade` runs first so security fixes Debian publishes AFTER the
# pinned base digest still land in the image (see agent-opencode.Dockerfile).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    iptables \
    jq \
    nftables \
    procps \
    && rm -rf /var/lib/apt/lists/*

# --- Node.js 22 (for Claude Code CLI) ---
# The nodesource `nodejs` package bundles `npm`, which the frontend acceptance
# gates (`npm test`) need in-container — no separate install required (#762).
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

# --- Claude Code CLI ---
# npm 12 blocks dependency install scripts unless they are allowlisted. Claude's
# wrapper can therefore install successfully while leaving only its failing
# "native binary not installed" stub. Install the matching native package with
# scripts disabled, then invoke only Claude's pinned installer explicitly.
#
# The final `test -x` is what docker/openshell/policy-claude-code.yaml stands
# on: that native binary, not node, is the process that owns the API socket, so
# an OpenShell policy that misnames it governs nothing and every contained
# dispatch fails closed on its first model call. Asserting it here turns the
# policy's path from a comment into a build-gated fact.
RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        arm64) native_package="@anthropic-ai/claude-code-linux-arm64" ;; \
        amd64) native_package="@anthropic-ai/claude-code-linux-x64" ;; \
        *) echo "unsupported Claude Code image architecture: $arch" >&2; exit 1 ;; \
    esac && \
    npm install -g --ignore-scripts \
        @anthropic-ai/claude-code@2.1.295 \
        "$native_package@2.1.295" && \
    node /usr/lib/node_modules/@anthropic-ai/claude-code/install.cjs && \
    test -x /usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe

# --- uv (Python package manager) ---
# Preinstalled so Python-repo agents run `uv run pytest` straight away instead
# of re-downloading uv into the bind-mounted /work every dispatch — wasted
# tokens/bandwidth and litter the agent must remember not to commit (#762).
# Installed into /usr/local/bin (on PATH for the agent user) rather than the
# per-run tmpfs HOME, so every dispatch finds it regardless of the ephemeral
# HOME. Runs while still root so the binary is root-owned on the read-only root.
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
# Create /work and chown to agent BEFORE `USER agent`, so the runtime user can
# write even when /work is not a bind mount (WORKDIR alone would leave it
# root-owned).
RUN useradd -m -s /bin/bash -u 1001 agent \
    && mkdir -p /work \
    && chown agent:agent /work

# --- Prompt-handoff entrypoint ---
# The host writes the prompt to <checkout>/.kew-prompt.txt (mounted at
# /work/.kew-prompt.txt); this entrypoint reads it and feeds it to
# `claude --print` on stdin. The full contract lives in
# docs/sandbox-entrypoint-contract.md; this image is its reference
# implementation (see docker/agent-entrypoint.sh).
# Copied while still root (before `USER agent`); --chmod sets the exec bit
# reproducibly without an extra `USER root` stanza.
COPY --chown=agent:agent --chmod=0755 docker/agent-entrypoint.sh /usr/local/bin/kew-agent-entrypoint

# --- OpenShell launcher ---
# Under `[sandbox] runtime = "openshell"` the ENTRYPOINT above never runs:
# `openshell sandbox exec` replaces it, so the host invokes this script instead
# and it does the same pre-runner work. Installed in BOTH agent images so one
# launcher serves both runners. See docker/kew-openshell-launch.
COPY --chown=agent:agent --chmod=0755 docker/kew-openshell-launch /usr/local/bin/kew-openshell-launch

# --- Publication producer (W1.1 D3) ---
# Extraction runs HERE, in a separate container over the finished checkout,
# because pointing host git at a directory an agent can write executes code from
# it (core.fsmonitor, filter.*.clean, a repo-local .git/config that cannot be
# suppressed). Inside this image that grants an attacker nothing new, and the
# host reads the result as opaque bytes. Contract: element 6 of
# docs/sandbox-entrypoint-contract.md.
COPY --chown=agent:agent --chmod=0755 docker/extract-artifact.sh /usr/local/bin/kew-extract-artifact

USER agent
WORKDIR /work

# Build-time startup guard. This runs as the final non-root user and catches a
# missing native package before a credential can ever reach the image.
RUN claude --version

ENTRYPOINT ["/usr/local/bin/kew-agent-entrypoint"]
