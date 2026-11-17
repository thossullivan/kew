# =============================================================================
# kew-switchyard
# Public-source build for the per-attempt credential-isolation proxy.
# =============================================================================

# The manifest resolves to these reviewed children:
# linux/arm64: sha256:b78cfbe4c0fd96b8a06b259092f1f412cb3ad3c5aed89d188bc40a1b8637f8f6
# linux/amd64: sha256:392f221964669402bf4ae133d2a46c3ae270f64734133f4b2703d40019235c1a
FROM rust:1.99.1-slim-bookworm@sha256:f2c004e3d2d8b547e8cf7ffbc29205385f4e71d23e53e4e383065db5e9f822cd AS builder

ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
        binutils \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && case "$TARGETARCH" in \
        amd64) rust_target=x86_64-unknown-linux-gnu; debian_arch=amd64 ;; \
        arm64) rust_target=aarch64-unknown-linux-gnu; debian_arch=arm64 ;; \
        *) echo 'unsupported proxy target architecture' >&2; exit 1 ;; \
    esac \
    && test "$(dpkg --print-architecture)" = "$debian_arch" \
    && rustc +1.99.1 --version | grep -Eq '^rustc 1\.99\.1 ' \
    && test "$(rustc +1.99.1 -vV | awk '/^host:/ {print $2}')" = "$rust_target"

# The source is upstream's public repository, fetched by full commit SHA and
# checked against that commit and its tree before anything reads it. The
# commit, tree and lockfile digests are part of this RUN definition, so
# changing any of them invalidates the cache. Source and Cargo output exist
# only on temporary mounts. An explicit +1.99.1 toolchain overrides upstream's
# rust-toolchain.toml, so the build uses the toolchain the builder image ships.
RUN --mount=type=tmpfs,target=/switchyard-source \
    --mount=type=tmpfs,target=/cargo-target \
    set -eu; \
    case "$TARGETARCH" in \
        amd64) rust_target=x86_64-unknown-linux-gnu ;; \
        arm64) rust_target=aarch64-unknown-linux-gnu ;; \
        *) echo 'unsupported proxy target architecture' >&2; exit 1 ;; \
    esac; \
    umask 077; \
    source_dir=/switchyard-source/src; \
    git init --quiet "$source_dir"; \
    git -C "$source_dir" fetch --quiet --depth 1 \
        https://github.com/NVIDIA-NeMo/Switchyard \
        46331725daebda02b83781f63bb9c94db9059a1e \
        2> /switchyard-source/fetch.log \
        || { echo 'switchyard source fetch failed' >&2; exit 1; }; \
    git -C "$source_dir" -c advice.detachedHead=false checkout --quiet FETCH_HEAD \
        || { echo 'switchyard source checkout failed' >&2; exit 1; }; \
    test "$(git -C "$source_dir" rev-parse HEAD)" \
        = 46331725daebda02b83781f63bb9c94db9059a1e \
        || { echo 'switchyard source commit mismatch' >&2; exit 1; }; \
    test "$(git -C "$source_dir" rev-parse 'HEAD^{tree}')" \
        = 41f84a3f62124058200c3603e999b1342b87019c \
        || { echo 'switchyard source tree mismatch' >&2; exit 1; }; \
    test -z "$(git -C "$source_dir" status --porcelain --untracked-files=all)" \
        || { echo 'switchyard source checkout is not clean' >&2; exit 1; }; \
    printf '%s  %s\n' \
        2e561fbd0cb752a12bed92e96ad817a0ab288365dcc7619b704f16400bdd4538 \
        "$source_dir/Cargo.lock" | sha256sum --check --status \
        || { echo 'switchyard source lockfile mismatch' >&2; exit 1; }; \
    curl --fail --silent --show-error --location \
        --output /switchyard-source/cargo-auditable.crate \
        https://static.crates.io/crates/cargo-auditable/cargo-auditable-0.7.5.crate; \
    printf '%s  %s\n' \
        cd121127b91d68074770a620544182345d7db56d03dcbd85316ab11e54a5b1bc \
        /switchyard-source/cargo-auditable.crate | sha256sum --check --status \
        || { echo 'cargo-auditable distribution digest mismatch' >&2; exit 1; }; \
    mkdir /switchyard-source/cargo-auditable; \
    tar -xzf /switchyard-source/cargo-auditable.crate \
        --strip-components=1 --directory=/switchyard-source/cargo-auditable; \
    jq --exit-status \
        '.git.sha1 == "9dc5ff28ff204b73eae91bd96cde4413fdcb70e0"' \
        /switchyard-source/cargo-auditable/.cargo_vcs_info.json >/dev/null \
        || { echo 'cargo-auditable revision mismatch' >&2; exit 1; }; \
    printf '%s  %s\n' \
        3a49de28391ca0e99709a96c64cd8e24f8f96d622f5a8360c2fbd5d8e0d9965e \
        /switchyard-source/cargo-auditable/Cargo.lock | sha256sum --check --status \
        || { echo 'cargo-auditable lockfile mismatch' >&2; exit 1; }; \
    cargo +1.99.1 metadata --locked --no-deps \
        --manifest-path /switchyard-source/cargo-auditable/Cargo.toml \
        --format-version=1 > /switchyard-source/cargo-auditable-metadata.json \
        2> /switchyard-source/cargo-auditable-metadata.log \
        || { echo 'cargo-auditable metadata validation failed' >&2; exit 1; }; \
    jq --exit-status \
        '.packages | length == 1 and \
         .[0].name == "cargo-auditable" and .[0].version == "0.7.5"' \
        /switchyard-source/cargo-auditable-metadata.json >/dev/null \
        || { echo 'cargo-auditable package identity mismatch' >&2; exit 1; }; \
    cargo +1.99.1 install --locked --path /switchyard-source/cargo-auditable \
        --root /usr/local --target "$rust_target" \
        || { echo 'cargo-auditable build failed' >&2; exit 1; }; \
    cargo +1.99.1 install --list --root /usr/local \
        > /switchyard-source/cargo-install-list \
        || { echo 'cargo-auditable installation metadata missing' >&2; exit 1; }; \
    grep -Fxq 'cargo-auditable v0.7.5 (/switchyard-source/cargo-auditable):' \
        /switchyard-source/cargo-install-list \
        || { echo 'cargo-auditable version mismatch' >&2; exit 1; }; \
    grep -Fxq '    cargo-auditable' /switchyard-source/cargo-install-list \
        || { echo 'cargo-auditable installed binary mismatch' >&2; exit 1; }; \
    cargo +1.99.1 auditable --help > /switchyard-source/cargo-auditable-help.log 2>&1 \
        || { echo 'cargo-auditable Cargo delegation failed' >&2; exit 1; }; \
    cd "$source_dir"; \
    cargo +1.99.1 metadata --locked --no-deps --format-version=1 \
        > /switchyard-source/metadata.json 2> /switchyard-source/metadata.log \
        || { echo 'switchyard package metadata validation failed' >&2; exit 1; }; \
    jq --exit-status \
        '.packages | any(.name == "switchyard-server" and .version == "0.3.0")' \
        /switchyard-source/metadata.json >/dev/null \
        || { echo 'switchyard package identity mismatch' >&2; exit 1; }; \
    export CARGO_TARGET_DIR=/cargo-target; \
    cargo +1.99.1 auditable build --locked --release --package switchyard-server \
        > /switchyard-source/cargo-build.log 2>&1 \
        || { tail -n 40 /switchyard-source/cargo-build.log >&2; \
             echo 'switchyard source compilation failed' >&2; exit 1; }; \
    binary=/cargo-target/release/switchyard-server; \
    test -x "$binary" \
        || { echo 'switchyard build produced no executable' >&2; exit 1; }; \
    readelf --sections --wide "$binary" | grep -Fq '.dep-v0' \
        || { echo 'switchyard binary lacks Rust audit metadata' >&2; exit 1; }; \
    strip --strip-debug "$binary"; \
    readelf --sections --wide "$binary" | grep -Fq '.dep-v0' \
        || { echo 'stripping removed Rust audit metadata' >&2; exit 1; }; \
    install -D --mode=0555 "$binary" /opt/kew/switchyard-server; \
    readelf --sections --wide /opt/kew/switchyard-server | grep -Fq '.dep-v0' \
        || { echo 'runtime Switchyard binary lacks Rust audit metadata' >&2; exit 1; }

FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS runtime

RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates libssl3t64 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/kew/switchyard-server /usr/local/bin/switchyard-server

LABEL org.opencontainers.image.source="https://github.com/NVIDIA-NeMo/Switchyard" \
      org.opencontainers.image.revision="46331725daebda02b83781f63bb9c94db9059a1e" \
      org.opencontainers.image.version="0.3.0" \
      org.kew.switchyard.lockfile-sha256="2e561fbd0cb752a12bed92e96ad817a0ab288365dcc7619b704f16400bdd4538" \
      org.kew.switchyard.package="switchyard-server" \
      org.kew.switchyard.rust-toolchain="1.99.1" \
      org.kew.switchyard.read-only-rootfs="required" \
      org.kew.switchyard.memory-limit="256m" \
      org.kew.switchyard.pids-limit="64" \
      org.kew.switchyard.cpu-limit="1"

USER 65532:65532
WORKDIR /
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/switchyard-server"]
