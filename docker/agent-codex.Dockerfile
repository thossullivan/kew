# =============================================================================
# kew-agent-codex
# Lean, non-root, credential-free image for dispatched Codex workers
# (the openshell-runtime pilot: `[sandbox] runtime = "openshell"`).
#
# There was no image with Codex in it at all. Codex dispatches run on the host
# under Codex's own seatbelt (`CodexRunner.containment_posture == "native"`),
# so kew never launched a container for them; the OpenShell pilot is the first
# boundary kew owns for this runner, and this is the image it launches.
#
# OPENSHELL-ONLY, deliberately. Under `[sandbox] runtime = "openshell"` the
# ENTRYPOINT below never runs — `openshell sandbox exec` replaces it and the
# host invokes /usr/local/bin/kew-openshell-launch instead, which is why this
# image installs the launcher and no agent entrypoint of its own. Making the
# launcher the ENTRYPOINT is not a second supported path: called by `docker
# run` with the host's container args, it reads the first of them as the
# startup nonce and refuses (exit 64) before any model call, which is the
# honest answer for an image whose docker lane does not exist yet. (The
# previous reference `docker/agent-codex-entrypoint.sh` was deleted with this
# node: it ran `codex exec "$@"` while CodexRunner.container_args() already
# sends `exec`, so the one argv it could have produced was `codex exec exec`.)
#
# Never bake credentials into this image. kew-openshell-launch writes the
# PLACEHOLDER ChatGPT-mode auth.json the pilot runs on; real credentials are
# the host provider's job (openshell-runtime N5).
# =============================================================================

# Pinned by digest for reproducibility and supply-chain integrity.
# debian:trixie-slim, manifest verified 2026-09-10.
# Same base as agent.Dockerfile and agent-opencode.Dockerfile, advanced with
# them so the three never diverge — tests/test_agent_image_scan_gate.py fails
# if one pin moves alone. To advance it: pull debian:trixie-slim, read the
# RepoDigest off the pulled image, paste it here in ALL THREE agent
# Dockerfiles, and let the CI agent-image-scan job re-scan what the new base
# ships.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS base

# Every RUN below runs under bash with pipefail (#2902). Under the default
# `/bin/sh -c`, a `curl ... | bash -` whose download fails pipes an empty
# script into bash, which exits 0, so the build carries on: an unreachable
# nodesource once let apt install Debian's own Node 20 in place of Node 22.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- System packages ---
# Same set as the other agent images: procps (#762) for fleet/process checks in
# repo test suites, jq (#797) for the security-guard hook payload.
#
# `apt-get upgrade` runs first so security fixes Debian publishes AFTER the
# pinned base digest still land in the image. Without it the base layer's
# packages are frozen at the digest's date, and the scan gate fails on every
# fix Debian ships in between (libexpat1 CVE-2026-56408, 2026-09-01).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    procps \
    && rm -rf /var/lib/apt/lists/*

# --- Node.js 22 (for the Codex CLI launcher and frontend acceptance gates) ---
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
# Identical to the other two agent images, and for the same reason: every
# fixable CRITICAL/HIGH Trivy reports lives under
# /usr/lib/node_modules/npm/node_modules -- npm's own vendored dependencies,
# shipped by the nodesource `nodejs` package. The two steps and the pinned npm
# are explained in full in docker/agent.Dockerfile; the short version is that
# upgrading npm clears most of them. npm 12.0.2 still vendors brace-expansion
# 5.0.7, ip-address 10.2.0, tar 7.5.19 and undici 6.27.0 despite published
# fixes, and `node kew-verify-npm-overlay` turns the overlay's reliance on npm's
# sibling packages into a build failure rather than a broken dispatch. On
# 2026-10-12, brace-expansion was bumped for CVE-2026-102276 and
# CVE-2026-102278, and undici was added for CVE-2026-19534.
# On 2026-10-24, undici moved to 6.29.1 for CVE-2026-109869.
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

# --- Codex CLI ---
# Pinned, not `@latest`: this is a supply-chain input inside the containment
# boundary, and an unreviewed Codex on any rebuild would be a change with no
# diff to review. Bump deliberately, with a smoke + one contained dispatch.
#
# `@openai/codex` ships ONE file of its own (bin/codex.js) and resolves the
# real executable out of a platform optionalDependency, so the native binary
# lives under an ARCH-SPECIFIC vendor directory:
#
#   .../node_modules/@openai/codex-linux-<arch>/vendor/<triple>/bin/codex
#
# `--ignore-scripts`: the package declares no install script, so nothing is
# skipped by refusing to run them — the vendor binary arrives executable in the
# platform tarball (verified on 0.160.1).
#
# The `test -x` is what docker/openshell/policy-codex.yaml stands on: that
# native binary, not node and not the npm shim, is the process that owns the
# API socket, so a policy that misnames it governs nothing and every contained
# dispatch fails closed on its first model call. Asserting it here turns the
# policy's path from a comment into a build-gated fact. The policy names BOTH
# arch paths (only one exists in any built image; a path that does not exist
# governs nothing and costs nothing), and both literal paths appear in the
# `case` below so tests/test_openshell_policy.py can require the two to agree.
RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        arm64) native_binary=/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex ;; \
        amd64) native_binary=/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex ;; \
        *) echo "unsupported Codex image architecture: $arch" >&2; exit 1 ;; \
    esac && \
    npm install -g --ignore-scripts @openai/codex@0.160.1 && \
    test -x "$native_binary" && \
    codex --version

# --- uv (Python package manager) ---
# Same rationale as the other agent images (#762): preinstalled on PATH so
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
    && mkdir -p /work \
    && chown agent:agent /work

# --- OpenShell launcher ---
# The only launch path this image has. It does the pre-runner work an
# entrypoint would (HOME out of the published checkout, git bootstrap, startup
# acknowledgement) plus Codex's own: writing the placeholder ChatGPT-mode
# $HOME/.codex/auth.json. See docker/kew-openshell-launch.
COPY --chown=agent:agent --chmod=0755 docker/kew-openshell-launch /usr/local/bin/kew-openshell-launch

# --- Publication producer (W1.1 D3) ---
# Every image a contained dispatch can run needs this, or publication silently
# depends on which runner was chosen. Same path and same contract as the other
# agent images — element 6 of docs/sandbox-entrypoint-contract.md.
COPY --chown=agent:agent --chmod=0755 docker/extract-artifact.sh /usr/local/bin/kew-extract-artifact

USER agent
WORKDIR /work

ENTRYPOINT ["/usr/local/bin/kew-openshell-launch"]
