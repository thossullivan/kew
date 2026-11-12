#!/usr/bin/env bash
# One checked-in recipe for a kew verification host — Ubuntu 24.04 LTS (noble).
#
# The verify VM this replaces was provisioned by hand from an operator script,
# so a second host, a sharded set of them or a CI runner could not be built the
# same way. This file is that recipe, in the repository, next to the provider
# that runs it (docs/hosts.md).
#
# What a host holds: Docker, Node 22, uv, git, the pinned trivy the agent-image
# gate IS, the Chromium headless shell that `ui-check` drives through
# playwright-core, and the tools the test suite asks for — ripgrep, ruby, perl,
# jq, lsof, an ssh client and the pinned gitleaks scripts/verify.sh refuses to run
# without: a merge verify under KEW_REQUIRE_TOOLS=1 refuses a test that skipped
# for want of a tool, so every tool in kew/tests/support/required_tools.py is
# installed here (#2763). What a host never holds: a GitHub, Linear, Google or model
# credential. Code arrives as a git bundle and results leave as data.
#
# It also creates the `kew-allowlist` Docker network an agent container attaches
# to when a dispatch run is placed on a host, only when it is absent, in the
# operator machine's shape: a plain bridge (driver bridge, not internal, no
# options). Without it the first placed dispatch on a fresh instance was
# refused until an operator created it by hand (#2607). A plain bridge has
# UNRESTRICTED egress until an operator restricts it, exactly as on the
# operator's own machine: this recipe installs no firewall rules, and an
# existing network — restricted or not — is never touched, so a re-run cannot
# undo an operator's restrictions (docs/hosts.md).
#
# Usage:
#
#     sudo bash ubuntu-24.04.sh          # install everything, then self-check
#     bash ubuntu-24.04.sh --self-check  # the self-check alone; needs no root
#     bash ubuntu-24.04.sh --allowlist-network  # the network step alone
#     bash ubuntu-24.04.sh --gitleaks    # the gitleaks step alone
#     bash ubuntu-24.04.sh --uv          # the uv step alone
#     bash ubuntu-24.04.sh --trivy       # the trivy step alone
#
# Every install step is idempotent, so re-running it on a provisioned host is
# a no-op that ends in the same self-check.
#
# The self-check prints one `KEW_HOST_TOOL <name>=<version>` line per tool and
# exits non-zero naming every tool it could NOT find, plus every tool whose
# version is not the one this recipe installs. A half-provisioned host — and a
# host carrying someone else's Node or playwright-core — therefore fails here,
# loudly, instead of failing a verification run an hour later; `kew hosts
# provision` reads those lines back as the host's recorded versions. It looks
# for every tool on KEW_JOB_PATH, the PATH a kew job gets, so a host that
# passes the self-check cannot fail every job with 127. The self-check uses
# bash builtins only — no `grep`, no `awk` — so it can be driven with nothing
# but the tools under test on PATH. The one exception is asking uv as the
# job's login (as_login_user), which reaches for `id` and `env`, and `sudo`
# when the recipe runs as root for another login.

set -euo pipefail

# Pinned where the drive pinned it: the playwright-core line ui-check's
# screenshots were taken against. Everything else tracks its distribution.
PLAYWRIGHT_VERSION="1.63.0"
NODE_MAJOR="22"

# The Python kew's suite is tested on: the version CI's pytest matrix pins in
# .github/workflows/ci.yml, which a test refuses to let drift from this one.
# Ubuntu 24.04 ships 3.12, so a job's `uv sync` either found no interpreter the
# project accepts or built on one CI does not test (#3238). uv installs it as a
# managed Python and pins it globally for the login the jobs run as
# (install_python), so a job's `uv sync` builds on it and the held-out runs,
# which ask for that environment's exact version, find the same one.
KEW_TESTED_PYTHON="3.13"

# The secrets scanner scripts/verify.sh runs, at the version it and CI pin
# (GITLEAKS_VERSION there and in .github/workflows/ci.yml; a test holds this
# one to verify.sh's). The version is part of that gate: an older build drops
# `.gitleaks.toml`'s `[[allowlists]]` without a word. Each archive's SHA-256 is
# copied from the release's gitleaks_<VER>_checksums.txt, as ci.yml's are, and
# verified before anything is extracted.
GITLEAKS_VERSION="8.30.0"
GITLEAKS_SHA256_LINUX_X64="79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e"
GITLEAKS_SHA256_LINUX_ARM64="b4cbbb6ddf7d1b2a603088cd03a4e3f7ce48ee7fd449b51f7de6ee2906f5fa2f"
# Where the verified binary goes. Overridable so a test can install into a
# directory of its own; the digests are not.
GITLEAKS_INSTALL_DIR="${GITLEAKS_INSTALL_DIR:-/usr/local/bin}"

# The scanner pin is NOT written here. It is read from `trivy.pin` beside this
# recipe — the same file `scripts/scan-agent-image.sh` reads the gate's pin from
# — because a host carrying any other trivy fails `kew verify` at the final
# agent-image scan, after the whole suite has already run, and a host carrying
# none cannot pass the gate at all (#2462). A second literal is how those two
# drifted in the first place.
#
# The pin travels with the recipe: `kew hosts provision` copies it up beside the
# recipe (kew/hosts/gcp.py), and a hand run finds it in the checkout. A missing
# pin is refused rather than defaulted — an unpinned scanner is a gate nobody
# chose. Overridable so a test can point this at a pin of its own.
#
# The directory is found with bash's own parameter expansion, not `dirname`: the
# self-check is driven with nothing but the tools under test on PATH, so this
# file reaches for no external binary before it has established what is there.
case "$0" in
    */*) RECIPE_DIR=${0%/*} ;;
    *) RECIPE_DIR=. ;;
esac
TRIVY_PIN_FILE="${TRIVY_PIN_FILE:-${RECIPE_DIR}/trivy.pin}"

# One `KEY=value` line of the pin file, read and not sourced — for the same
# reason os_codename below reads /etc/os-release this way: a sourced file puts
# whatever it contains into this shell, and shellcheck cannot follow it. The pin
# file is comments and plain assignments, which is all this understands.
pin_value() {
    local key=$1 line
    while IFS= read -r line; do
        case "$line" in
            "$key"=*)
                printf '%s\n' "${line#"$key"=}"
                return 0
                ;;
        esac
    done <"$TRIVY_PIN_FILE"
    printf 'cannot read %s from %s: this recipe installs the trivy the agent-image gate pins, and reads that pin from trivy.pin beside it\n' \
        "$key" "$TRIVY_PIN_FILE" >&2
    return 1
}

if [ ! -f "$TRIVY_PIN_FILE" ]; then
    printf 'no scanner pin at %s: this recipe installs the trivy the agent-image gate pins, and reads that version from trivy.pin beside it\n' \
        "$TRIVY_PIN_FILE" >&2
    exit 2
fi
TRIVY_VERSION=$(pin_value TRIVY_VERSION) || exit 2
# uv is pinned in the same file, by version and per-architecture archive digest
# (install_uv), so the one file that travels with the recipe holds every pin
# it installs from a release archive.
UV_VERSION=$(pin_value UV_VERSION) || exit 2
# Where the verified uv, uvx and trivy go. Overridable so a test can install
# into a directory of its own; the digests are not. A kew-prefixed name of its
# own, never astral's UV_INSTALL_DIR: an operator environment that sets that
# for the astral installer must not move where this recipe puts its binaries.
KEW_PROVISION_BIN_DIR="${KEW_PROVISION_BIN_DIR:-/usr/local/bin}"

# Browsers go where every login can read them rather than into one user's
# ~/.cache: a job arrives as whichever SSH user the host accepts, which is not
# always the user that provisioned it. Overridable so the self-check can be
# pointed at a stub tree.
PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/playwright-browsers}"
export PLAYWRIGHT_BROWSERS_PATH

# The PATH a kew *job* runs under on this host, and therefore the PATH the
# self-check below looks for the tools on. A job arrives over `gcloud compute
# ssh --command`: a non-interactive, non-login shell that sources no profile,
# so ~/.local/bin — where a curl-installed tool lands, and where only a login
# shell's profile puts it — is not on its PATH unless something says so. A
# verify job died with `uv: not found`, exit 127, on a host whose own
# provisioning had passed (#2433). Checking the tools from the caller's PATH
# instead of this one is what let the two disagree.
#
# kew passes the job's own value in (REMOTE_PATH in kew/hosts/environment.py,
# which a test refuses to let drift from this default); the default is the same
# list, for an operator running this file by hand.
KEW_JOB_PATH="${KEW_JOB_PATH:-$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin}"

# The tools a verification host must have, as `name:pin:command`, in the order
# the self-check prints them.
#
# The *pin* is the difference between "something called node is installed" and
# "the Node this recipe installs is installed": `any` takes whatever the
# distribution ships, `major N` takes N and any N.x, `minor N.M` takes N.M and
# any N.M.x, `exact V` takes only V. A host answering off the pin fails the
# self-check rather than being recorded by kew as built from this recipe. The
# pins are this file's own NODE_MAJOR, KEW_TESTED_PYTHON and
# PLAYWRIGHT_VERSION and the TRIVY_VERSION it read out of trivy.pin, so the
# check cannot drift from what the recipe installs — and a host whose trivy is
# not the gate's trivy fails provisioning here rather than failing every
# verification at the image scan (#2462).
#
# The command may be a shell function defined below, which is how the browser
# and docker's *server* version are checked — and how the two steps that are
# not a tool are: `python` is the Python the job's login gets from uv outside
# any project, held to KEW_TESTED_PYTHON (install_python), and
# `logind-keep-ipc` is systemd's version, answered only while keep_ipc's
# RemoveIPC=no drop-in is in place.
TOOL_CHECKS=(
    "git:any:git --version"
    "docker:any:docker_server_version"
    "node:major ${NODE_MAJOR}:node --version"
    "npm:any:npm --version"
    "uv:any:uv --version"
    "python:minor ${KEW_TESTED_PYTHON}:job_python_version"
    "trivy:exact ${TRIVY_VERSION}:trivy --version"
    "playwright-core:exact ${PLAYWRIGHT_VERSION}:playwright-core --version"
    "chromium-headless-shell:any:headless_shell_build"
    "ripgrep:any:rg --version"
    "ruby:any:ruby --version"
    "perl:any:perl_version"
    "jq:any:jq_version"
    "lsof:any:lsof_version"
    "ssh:any:ssh_version"
    "gitleaks:exact ${GITLEAKS_VERSION}:gitleaks version"
    "logind-keep-ipc:any:logind_keep_ipc"
)

log() {
    printf '==> %s\n' "$*"
}

usage() {
    printf 'usage: %s [--self-check | --allowlist-network | --gitleaks | --uv | --trivy | --python | --keep-ipc]\n' "${0##*/}"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        printf 'this recipe installs system packages: run it as root (sudo bash %s)\n' \
            "${0##*/}" >&2
        exit 2
    fi
}

apt_install() {
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

# The release codename, read without sourcing /etc/os-release: a sourced file
# puts whatever it contains into this shell, and shellcheck cannot follow it.
os_codename() {
    local line
    while IFS= read -r line; do
        case "$line" in
            VERSION_CODENAME=*)
                line=${line#VERSION_CODENAME=}
                printf '%s\n' "${line//\"/}"
                return 0
                ;;
        esac
    done </etc/os-release
    printf 'cannot read VERSION_CODENAME from /etc/os-release\n' >&2
    return 1
}

install_base() {
    log "base packages"
    apt-get update
    apt_install ca-certificates curl git gnupg ripgrep ruby perl jq lsof openssh-client
}

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        log "docker is already installed"
    else
        log "docker from download.docker.com"
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] %s %s stable\n' \
            "$(dpkg --print-architecture)" \
            "https://download.docker.com/linux/ubuntu" \
            "$(os_codename)" >/etc/apt/sources.list.d/docker.list
        apt-get update
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
            docker-compose-plugin
    fi
    # Outside the branch, and idempotent: the self-check asks docker for its
    # *server* version, so a host whose daemon is installed but stopped is one
    # this recipe has to start, not one it can walk past.
    systemctl enable --now docker
}

# A job that runs `docker` over SSH is not root, so the login needs the group.
# A new membership applies at the next login, which is the one after this run.
grant_docker_access() {
    local operator=${SUDO_USER:-}
    if [ -z "$operator" ] || [ "$operator" = "root" ]; then
        return 0
    fi
    log "adding ${operator} to the docker group"
    usermod -aG docker "$operator"
}

# The egress network a placed agent container attaches (ALLOWLIST_NETWORK in
# kew/sandbox.py), created in the operator machine's shape when absent and left
# exactly as it is when present: an operator's restrictions on an existing
# network are theirs, and a re-run must not swap them for a plain bridge. Runs
# after install_docker, whose daemon it asks.
ALLOWLIST_NETWORK="kew-allowlist"

create_allowlist_network() {
    if docker network inspect "$ALLOWLIST_NETWORK" >/dev/null 2>&1; then
        log "docker network ${ALLOWLIST_NETWORK} already exists"
        return 0
    fi
    log "docker network ${ALLOWLIST_NETWORK}: a plain bridge, egress operator-managed"
    docker network create --driver bridge "$ALLOWLIST_NETWORK" >/dev/null
}

install_node() {
    if [ -n "$(node_major)" ] && [ "$(node_major)" = "$NODE_MAJOR" ]; then
        log "node ${NODE_MAJOR} is already installed"
        return 0
    fi
    log "node ${NODE_MAJOR} from deb.nodesource.com"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        -o /etc/apt/keyrings/nodesource.asc
    chmod a+r /etc/apt/keyrings/nodesource.asc
    printf 'deb [signed-by=/etc/apt/keyrings/nodesource.asc] %s nodistro main\n' \
        "https://deb.nodesource.com/node_${NODE_MAJOR}.x" \
        >/etc/apt/sources.list.d/nodesource.list
    apt-get update
    apt_install nodejs
}

node_major() {
    local reported
    reported=$(node --version 2>/dev/null) || return 0
    reported=${reported#v}
    printf '%s\n' "${reported%%.*}"
}

# The uv every verify job runs, at the UV_VERSION trivy.pin names, from the
# release archive whose SHA-256 that same file pins, verified on disk before
# anything is extracted. Never the astral.sh installer script: that is a
# program whose bytes are whatever the URL serves on the day, and running it
# as root installs a uv nothing here identified (#2631).
#
# Installed on every run, never skipped because a uv already answers: for the
# reason install_gitleaks gives, the verified archive is what says which uv
# this is, so it is what ends up on PATH.
install_uv() {
    local target digest sha archive
    case "$(uname -m)" in
        x86_64)
            target="x86_64-unknown-linux-gnu"
            digest="UV_SHA256_X86_64_LINUX"
            ;;
        aarch64 | arm64)
            target="aarch64-unknown-linux-gnu"
            digest="UV_SHA256_AARCH64_LINUX"
            ;;
        *)
            printf 'no uv build is pinned for %s\n' "$(uname -m)" >&2
            return 1
            ;;
    esac
    sha=$(pin_value "$digest") || return 1
    log "uv ${UV_VERSION} (${target}) from github.com/astral-sh/uv"
    archive=$(mktemp)
    curl -fsSL -o "$archive" \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz"
    if ! printf '%s  %s\n' "$sha" "$archive" | sha256sum -c - >/dev/null; then
        rm -f "$archive"
        printf 'the downloaded uv archive does not match the SHA-256 pinned in %s: refusing to install a uv this recipe cannot identify\n' \
            "$TRIVY_PIN_FILE" >&2
        return 1
    fi
    # The archive holds uv-<target>/uv and uv-<target>/uvx, one directory down.
    tar xzf "$archive" -C "$KEW_PROVISION_BIN_DIR" --strip-components=1 \
        "uv-${target}/uv" "uv-${target}/uvx"
    chmod 0755 "$KEW_PROVISION_BIN_DIR/uv" "$KEW_PROVISION_BIN_DIR/uvx"
    rm -f "$archive"
}

# The scanner the agent-image gate IS, at the version `trivy.pin` names, from
# the release archive whose SHA-256 that same file pins. Not `apt install trivy`
# and not the upstream install script: both of them take whatever the channel
# currently holds, and a host whose scanner is one minor off fails `kew verify`
# at the last step of the run (#2462).
#
# The archive lands on disk and is verified BEFORE anything is extracted. A
# version string is self-reported — a substituted archive answers with the
# pinned version and returns clean scans forever — so the digest is the only
# thing here that says which scanner this is. For the same reason the step runs
# every time, never skipped because the trivy on the host answers
# `trivy --version` with the pin (#3258): the gate refuses a binary that is not
# the pinned archive's, so a re-run replaces it with one that is.
install_trivy() {
    local asset digest sha archive
    case "$(uname -m)" in
        x86_64)
            asset="Linux-64bit"
            digest="TRIVY_SHA256_LINUX_64BIT"
            ;;
        aarch64 | arm64)
            asset="Linux-ARM64"
            digest="TRIVY_SHA256_LINUX_ARM64"
            ;;
        *)
            printf 'no trivy build is pinned for %s\n' "$(uname -m)" >&2
            return 1
            ;;
    esac
    sha=$(pin_value "$digest") || return 1
    log "trivy ${TRIVY_VERSION} (${asset}) from github.com/aquasecurity/trivy"
    # A fresh name per run, so an interrupted download leaves nothing a later
    # run could mistake for the archive it just fetched.
    archive=$(mktemp)
    curl -fsSL -o "$archive" \
        "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_${asset}.tar.gz"
    if ! printf '%s  %s\n' "$sha" "$archive" | sha256sum -c - >/dev/null; then
        rm -f "$archive"
        printf 'the downloaded trivy archive does not match the SHA-256 pinned in %s: refusing to install a scanner this recipe cannot identify\n' \
            "$TRIVY_PIN_FILE" >&2
        return 1
    fi
    tar xzf "$archive" -C "$KEW_PROVISION_BIN_DIR" trivy
    chmod 0755 "$KEW_PROVISION_BIN_DIR/trivy"
    rm -f "$archive"
}

# The secrets scanner verify.sh runs, at GITLEAKS_VERSION, from the release
# archive whose SHA-256 this file pins — verified on disk before extraction, for
# the reason install_trivy gives.
#
# Installed on every run, never skipped because a gitleaks already on the host
# answers `gitleaks version` with the pin: that answer is self-reported, so a
# substituted binary printing 8.30.0 would walk past the digest and gate every
# verify with a scanner nobody identified (#2867). The verified archive is what
# says which scanner this is, so it is what ends up on PATH, every time.
install_gitleaks() {
    local asset sha archive
    case "$(uname -m)" in
        x86_64)
            asset="linux_x64"
            sha=$GITLEAKS_SHA256_LINUX_X64
            ;;
        aarch64 | arm64)
            asset="linux_arm64"
            sha=$GITLEAKS_SHA256_LINUX_ARM64
            ;;
        *)
            printf 'no gitleaks build is pinned for %s\n' "$(uname -m)" >&2
            return 1
            ;;
    esac
    log "gitleaks ${GITLEAKS_VERSION} (${asset}) from github.com/gitleaks/gitleaks"
    archive=$(mktemp)
    curl -fsSL -o "$archive" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_${asset}.tar.gz"
    if ! printf '%s  %s\n' "$sha" "$archive" | sha256sum -c - >/dev/null; then
        rm -f "$archive"
        printf 'the downloaded gitleaks archive does not match the SHA-256 pinned in this recipe: refusing to install a scanner it cannot identify\n' >&2
        return 1
    fi
    tar xzf "$archive" -C "$GITLEAKS_INSTALL_DIR" gitleaks
    chmod 0755 "$GITLEAKS_INSTALL_DIR/gitleaks"
    rm -f "$archive"
}

# logind removes every System V and POSIX IPC object a login owns — its files
# under /dev/shm among them — when that login's last session ends
# (RemoveIPC=yes, the default). kew ships a verify bundle to /dev/shm/kew-host
# in one ssh session and clones it in the next (_REMOTE_BUNDLE_DIR in
# kew/hosts/gcp.py), so on a stock host the bundle was gone before the job
# could read it (#3238). A drop-in turns the removal off, and logind is
# reloaded so the host needs no reboot to take it.
KEW_LOGIND_CONF_DIR="${KEW_LOGIND_CONF_DIR:-/etc/systemd/logind.conf.d}"

keep_ipc() {
    log "logind keeps a login's IPC and /dev/shm files between its sessions"
    mkdir -p "$KEW_LOGIND_CONF_DIR"
    printf '[Login]\nRemoveIPC=no\n' >"$KEW_LOGIND_CONF_DIR/kew-keep-ipc.conf"
    systemctl try-reload-or-restart systemd-logind
}

# The login a job runs as: whoever ran this recipe through sudo, else this
# shell's own user.
login_user() {
    local operator=${SUDO_USER:-}
    if [ -z "$operator" ] || [ "$operator" = "root" ]; then
        operator=$(id -un)
    fi
    printf '%s\n' "$operator"
}

# Run a command as the login a job runs as, on the PATH a job gets, so what it
# writes under that login's home belongs to that login.
as_login_user() {
    local operator
    operator=$(login_user)
    if [ "$(id -un)" = "$operator" ]; then
        env PATH="$KEW_JOB_PATH" "$@"
    else
        sudo -u "$operator" -H env PATH="$KEW_JOB_PATH" "$@"
    fi
}

install_python() {
    log "python ${KEW_TESTED_PYTHON} through uv, pinned for $(login_user)"
    as_login_user uv python install "$KEW_TESTED_PYTHON"
    as_login_user uv python pin --global "$KEW_TESTED_PYTHON"
}

install_browser() {
    log "playwright-core ${PLAYWRIGHT_VERSION} and the chromium headless shell"
    npm install --global --no-fund --no-audit \
        "playwright-core@${PLAYWRIGHT_VERSION}"
    mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
    # --with-deps brings in the shared libraries headless chromium needs; it
    # installs apt packages, which is the other reason this step wants root.
    playwright-core install --with-deps chromium-headless-shell
    # Every login finds the shared browser directory, not just this one.
    printf 'export PLAYWRIGHT_BROWSERS_PATH=%s\n' "$PLAYWRIGHT_BROWSERS_PATH" \
        >/etc/profile.d/kew-host.sh
}

# The build number of the installed headless shell, from the directory
# playwright puts it in. A browser is not on PATH, so this is the version
# command for it. Playwright has laid the shell out two ways: the current
# chrome-headless-shell-linux64/chrome-headless-shell and the old
# chrome-linux/headless_shell (#3238).
headless_shell_build() {
    local candidate build
    for candidate in \
        "$PLAYWRIGHT_BROWSERS_PATH"/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell \
        "$PLAYWRIGHT_BROWSERS_PATH"/chromium_headless_shell-*/chrome-linux/headless_shell; do
        if [ -x "$candidate" ]; then
            build=${candidate#"$PLAYWRIGHT_BROWSERS_PATH"/chromium_headless_shell-}
            printf '%s\n' "${build%%/*}"
            return 0
        fi
    done
    return 1
}

# The docker *server* version, which is the version command for docker here
# because a client alone cannot answer it. `docker --version` prints the CLI's
# own version and exits 0 while dockerd is dead or unreachable, and a host
# whose docker cannot start a container is not a provisioned host — it is one
# that fails a verification run an hour later.
docker_server_version() {
    docker version --format '{{.Server.Version}}'
}

# Version commands for the tools whose own answer first_version cannot read:
# jq says `jq-1.7.1`, lsof and ssh answer on stderr — ssh as
# `OpenSSH_9.6p1 ...` — and perl's banner leads with `perl 5,` before its real
# version. Each says nothing at all when its tool is absent.
jq_version() {
    local reported
    reported=$(jq --version) || return 1
    printf '%s\n' "${reported#jq-}"
}

perl_version() {
    # shellcheck disable=SC2016 # $^V is perl's, not this shell's
    perl -e 'printf "%vd\n", $^V'
}

lsof_version() {
    command -v lsof >/dev/null || return 1
    lsof -v 2>&1 || true
}

ssh_version() {
    local reported
    reported=$(ssh -V 2>&1) || return 1
    printf '%s\n' "${reported#OpenSSH_}"
}

# The version of the Python the job's login gets from uv with no project
# around it, which is what install_python pinned: asked as that login, from
# `/`, so neither root's own uv state nor a project or `.python-version` in
# the caller's directory answers for it. A subshell, so the `cd` stays in it.
job_python_version() (
    cd / && as_login_user uv python find --show-version
)

# systemd's version, answered only while the drop-in keep_ipc writes still
# says RemoveIPC=no: a host without it loses the shipped bundle between a
# job's ssh sessions (#3238), so it is not a provisioned host.
logind_keep_ipc() {
    local line drop_in="$KEW_LOGIND_CONF_DIR/kew-keep-ipc.conf"
    [ -f "$drop_in" ] || return 1
    while IFS= read -r line; do
        if [ "$line" = "RemoveIPC=no" ]; then
            systemctl --version
            return
        fi
    done <"$drop_in"
    return 1
}

# The first version-shaped token in a tool's own version line, as bash sees it:
# `Docker version 27.3.1, build ce12230` is 27.3.1 and `v22.9.0` is 22.9.0.
first_version() {
    local token
    for token in $1; do
        if [[ $token =~ ^v?([0-9]+(\.[0-9]+)*) ]]; then
            printf '%s\n' "${BASH_REMATCH[1]}"
            return 0
        fi
    done
    return 1
}

# Whether *version* satisfies *pin*, where the pin is one of this recipe's own
# (see TOOL_CHECKS). An unrecognized pin is refused rather than waved through:
# a typo in the table must not read as "anything goes".
version_satisfies() {
    local version=$1 pin=$2
    case "$pin" in
        any) return 0 ;;
        "major "*) [ "${version%%.*}" = "${pin#major }" ] ;;
        "minor "*) [[ "$version." == "${pin#minor }".* ]] ;;
        "exact "*) [ "$version" = "${pin#exact }" ] ;;
        *) return 1 ;;
    esac
}

# One `name pin command` entry: print its KEW_HOST_TOOL line and return 0, or
# return 1 (no version at all) or 2 (a version, but not this recipe's).
check_tool() {
    local name=$1 pin=$2 output version
    local -a argv
    read -r -a argv <<<"$3"
    output=$("${argv[@]}" 2>/dev/null) || output=""
    version=$(first_version "$output") || version=""
    if [ -z "$version" ]; then
        return 1
    fi
    if ! version_satisfies "$version" "$pin"; then
        printf 'kew host self-check: %s reports %s, but this recipe installs %s\n' \
            "$name" "$version" "$pin" >&2
        return 2
    fi
    printf 'KEW_HOST_TOOL %s=%s\n' "$name" "$version"
}

# Print every tool's version, and fail naming the ones that are absent or off
# this recipe's pins, looking for them on the PATH a job gets rather than the
# one this shell happens to have. This is the whole point of a checked-in
# recipe: the host says what it has, and kew records it only when the two
# agree — and "has" has to mean "a job can run it".
self_check() {
    local missing=() wrong=() entry name rest status
    # The job's PATH, not the caller's: a tool only a login shell can reach is
    # a tool every job fails on, so it has to be missing *here* (#2433).
    local PATH="$KEW_JOB_PATH"
    for entry in "${TOOL_CHECKS[@]}"; do
        name=${entry%%:*}
        rest=${entry#*:}
        status=0
        check_tool "$name" "${rest%%:*}" "${rest#*:}" || status=$?
        case "$status" in
            1) missing+=("$name") ;;
            2) wrong+=("$name") ;;
        esac
    done
    if [ "${#missing[@]}" -ne 0 ]; then
        printf 'kew host self-check FAILED: missing %s\n' "${missing[*]}" >&2
    fi
    if [ "${#wrong[@]}" -ne 0 ]; then
        printf 'kew host self-check FAILED: wrong version %s\n' "${wrong[*]}" >&2
    fi
    if [ "${#missing[@]}" -ne 0 ] || [ "${#wrong[@]}" -ne 0 ]; then
        return 1
    fi
    printf 'kew host self-check OK: %s tools\n' "${#TOOL_CHECKS[@]}"
}

install_all() {
    require_root
    install_base
    install_docker
    create_allowlist_network
    grant_docker_access
    install_node
    install_uv
    install_python
    install_trivy
    install_gitleaks
    install_browser
    keep_ipc
}

main() {
    case "${1:-install}" in
        install)
            install_all
            self_check
            ;;
        --self-check)
            self_check
            ;;
        --allowlist-network)
            create_allowlist_network
            ;;
        --gitleaks)
            install_gitleaks
            ;;
        --uv)
            install_uv
            ;;
        --trivy)
            install_trivy
            ;;
        --python)
            install_python
            ;;
        --keep-ipc)
            keep_ipc
            ;;
        -h | --help)
            usage
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
