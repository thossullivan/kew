# =============================================================================
# kew-switchyard
# Private-source build for the per-attempt credential-isolation proxy.
# =============================================================================

# The manifest resolves to these reviewed children:
# linux/arm64: sha256:e42d574c21b0e1348205dc0e8994086cf73088596e999f67fce215f8406881a4
# linux/amd64: sha256:39decc9f9f4a87e03db5069b2424c0ee13c3d7e4116010980d4f96849e823bc4
FROM rust:1.96.1-slim-bookworm@sha256:e18a79fc84dfcfc3ab5ba72290398a644c135c97eaa881447fddc354ee4701a3 AS builder

ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
        binutils \
        build-essential \
        ca-certificates \
        curl \
        jq \
        libssl-dev \
        python3 \
        pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && case "$TARGETARCH" in \
        amd64) rust_target=x86_64-unknown-linux-gnu; debian_arch=amd64 ;; \
        arm64) rust_target=aarch64-unknown-linux-gnu; debian_arch=arm64 ;; \
        *) echo 'unsupported proxy target architecture' >&2; exit 1 ;; \
    esac \
    && test "$(dpkg --print-architecture)" = "$debian_arch" \
    && rustc +1.96.1 --version | grep -Eq '^rustc 1\.96\.1 ' \
    && test "$(rustc +1.96.1 -vV | awk '/^host:/ {print $2}')" = "$rust_target"

# BuildKit must provide the reviewed archive as a secret. Source, Cargo output,
# and private diagnostics exist only on temporary mounts. The fixed archive
# digest is part of this RUN definition, so changing it invalidates the cache.
# The host gate validates the same archive on every invocation, including when
# this instruction itself is satisfied from cache.
RUN --mount=type=secret,id=switchyard_source_archive_00,required=true \
    --mount=type=secret,id=switchyard_source_archive_01,required=true \
    --mount=type=secret,id=switchyard_source_archive_02,required=true \
    --mount=type=secret,id=switchyard_source_archive_03,required=true \
    --mount=type=secret,id=switchyard_source_archive_04,required=true \
    --mount=type=secret,id=switchyard_source_archive_05,required=true \
    --mount=type=secret,id=switchyard_source_archive_06,required=true \
    --mount=type=secret,id=switchyard_source_archive_07,required=true \
    --mount=type=secret,id=switchyard_source_archive_08,required=true \
    --mount=type=secret,id=switchyard_source_archive_09,required=true \
    --mount=type=secret,id=switchyard_source_archive_10,required=true \
    --mount=type=secret,id=switchyard_source_archive_11,required=true \
    --mount=type=secret,id=switchyard_source_archive_12,required=true \
    --mount=type=secret,id=switchyard_source_archive_13,required=true \
    --mount=type=secret,id=switchyard_source_archive_14,required=true \
    --mount=type=secret,id=switchyard_source_archive_15,required=true \
    --mount=type=tmpfs,target=/private-source \
    --mount=type=tmpfs,target=/cargo-target \
    set -eu; \
    case "$TARGETARCH" in \
        amd64) rust_target=x86_64-unknown-linux-gnu ;; \
        arm64) rust_target=aarch64-unknown-linux-gnu ;; \
        *) echo 'unsupported proxy target architecture' >&2; exit 1 ;; \
    esac; \
    umask 077; \
    archive=/private-source/source.tar; \
    (set -C; : > "$archive") 2> /private-source/chunk-transport.log \
        || { echo 'switchyard private chunk transport failed' >&2; exit 1; }; \
    for suffix in 00 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15; do \
        part="/run/secrets/switchyard_source_archive_$suffix"; \
        expected=491520; \
        if [ "$suffix" = 15 ]; then expected=81920; fi; \
        { test "$(wc -c < "$part")" -eq "$expected" && cat "$part" >> "$archive"; } \
            2> /private-source/chunk-transport.log \
            || { echo 'switchyard private chunk transport failed' >&2; exit 1; }; \
    done; \
    test "$(wc -c < "$archive")" -eq 7454720 \
        || { echo 'switchyard source archive length mismatch' >&2; exit 1; }; \
    printf '%s  %s\n' \
        a53aac972293ceef2ac2f5a84da92e022cc1d4a903521761cc90f18655cfb31e \
        "$archive" | sha256sum --check --status \
        || { echo 'switchyard source archive digest mismatch' >&2; exit 1; }; \
    python3 -I -B -c 'import hashlib,json,re,sys,tarfile;from pathlib import PurePosixPath;a=tarfile.open(sys.argv[1],"r:*");m=a.getmembers();n=[x.name for x in m];good=lambda s:isinstance(s,str) and bool(s) and not s.startswith("/") and "\\" not in s and all(c not in ("",".","..") for c in s.split("/")) and PurePosixPath(s).as_posix()==s and not any(ord(c)<32 or ord(c)==127 for c in s);all(good(x) for x in n) or sys.exit(1);len(n)==len(set(n)) or sys.exit(1);all((x.isdir() or x.isreg()) and not x.pax_headers and x.uid==0 and x.gid==0 and x.mtime==0 for x in m) or sys.exit(1);mb={x.name:x for x in m};u=mb.get("SOURCE-BINDING.json");u is not None and u.isreg() and u.mode==384 or sys.exit(1);f=a.extractfile(u);f is not None or sys.exit(1);z=f.read();hashlib.sha256(z).hexdigest()==sys.argv[2] or sys.exit(1);b=json.loads(z.decode("utf-8"));c=b.get("membership_contract");q=b.get("archive");r=b.get("files");isinstance(c,dict) and isinstance(q,dict) and isinstance(r,list) or sys.exit(1);am=c.get("auxiliary_member");root=c.get("source_root");am=={"mode":"100600","name":"SOURCE-BINDING.json","type":"regular-file"} and q.get("format")=="ustar" and q.get("root")==root and good(root) and "/" not in root or sys.exit(1);len(r)==563 or sys.exit(1);modes={"100644":420,"100755":493};all(isinstance(x,dict) and good(x.get("path")) and x.get("mode") in modes and isinstance(x.get("sha256"),str) and re.fullmatch("[0-9a-f]{64}",x.get("sha256")) for x in r) or sys.exit(1);p=[x["path"] for x in r];len(p)==len(set(p)) or sys.exit(1);set(n)=={root,"SOURCE-BINDING.json"}|{root+"/"+x for x in p} or sys.exit(1);mb[root].isdir() and mb[root].mode==493 or sys.exit(1);all(mb[root+"/"+x["path"]].isreg() and mb[root+"/"+x["path"]].mode==modes[x["mode"]] and hashlib.sha256(a.extractfile(mb[root+"/"+x["path"]]).read()).hexdigest()==x["sha256"] for x in r) or sys.exit(1);print(root,end="")' \
        "$archive" 278454c787735a25bf788701a9d2bba786555c09ba918af25e6c499d9c82609b \
        > /private-source/payload-root 2> /private-source/archive-validate.log \
        || { echo 'switchyard source archive member validation failed' >&2; exit 1; }; \
    payload_root="$(cat /private-source/payload-root)"; \
    python3 -I -B -c 'import json,re,sys,tarfile;a=tarfile.open(sys.argv[1],"r:*");m=a.getmembers();mb={x.name:x for x in m};u=mb["SOURCE-BINDING.json"];u.type==tarfile.REGTYPE and u.uname==u.gname=="" or sys.exit(1);f=a.extractfile(u);f is not None or sys.exit(1);b=json.loads(f.read().decode("utf-8"));root=b["membership_contract"]["source_root"];rows=b["files"];allowed={"base-blob","approved-replacement"};all(x.type in (tarfile.DIRTYPE,tarfile.REGTYPE) and x.uid==0 and x.gid==0 and x.uname==x.gname=="" and x.mtime==0 for x in m) or sys.exit(1);all(set(r)=={"base_blob","mode","path","sha256","size","source"} and isinstance(r["size"],int) and r["size"]>=0 and isinstance(r["base_blob"],str) and re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}",r["base_blob"]) and r["source"] in allowed for r in rows) or sys.exit(1);all(mb[root+"/"+r["path"]].type==tarfile.REGTYPE and mb[root+"/"+r["path"]].size==r["size"] for r in rows) or sys.exit(1)' \
        "$archive" 2> /private-source/archive-metadata.log \
        || { echo 'switchyard source archive member validation failed' >&2; exit 1; }; \
    tar -xf "$archive" --directory=/private-source \
        2> /private-source/archive-extract.log \
        || { echo 'switchyard source archive cannot be extracted' >&2; exit 1; }; \
    test -d "/private-source/$payload_root" \
        || { echo 'switchyard source payload is missing' >&2; exit 1; }; \
    test -z "$(find /private-source -type l -print -quit)" \
        || { echo 'switchyard source archive contains a link' >&2; exit 1; }; \
    test "$(find "/private-source/$payload_root" -type f | wc -l)" = 563 \
        || { echo 'switchyard source payload file count mismatch' >&2; exit 1; }; \
    printf '%s  %s\n' \
        278454c787735a25bf788701a9d2bba786555c09ba918af25e6c499d9c82609b \
        /private-source/SOURCE-BINDING.json | sha256sum --check --status \
        || { echo 'switchyard source manifest digest mismatch' >&2; exit 1; }; \
    for binding in \
        https://github.com/NVIDIA-NeMo/Switchyard \
        6ff491acd7eb1ec239525a1baa015730c3d3564a \
        49b34765d221181c6870eabcecb1b706baf21d46 \
        e895886482133481679db318c824919d5502166b8ef3564f87c9f825d0d8e0ab \
        switchyard-server \
        0.1.0 \
        1.96.1; do \
        jq --exit-status --arg value "$binding" \
            '[.. | strings | select(. == $value)] | length > 0' \
            /private-source/SOURCE-BINDING.json >/dev/null \
            || { echo 'switchyard source manifest binding mismatch' >&2; exit 1; }; \
    done; \
    source_dir="/private-source/$payload_root"; \
    printf '%s  %s\n' \
        e895886482133481679db318c824919d5502166b8ef3564f87c9f825d0d8e0ab \
        "$source_dir/Cargo.lock" | sha256sum --check --status \
        || { echo 'switchyard source lockfile mismatch' >&2; exit 1; }; \
    curl --fail --silent --show-error --location \
        --output /private-source/cargo-auditable.crate \
        https://static.crates.io/crates/cargo-auditable/cargo-auditable-0.7.5.crate; \
    printf '%s  %s\n' \
        cd121127b91d68074770a620544182345d7db56d03dcbd85316ab11e54a5b1bc \
        /private-source/cargo-auditable.crate | sha256sum --check --status \
        || { echo 'cargo-auditable distribution digest mismatch' >&2; exit 1; }; \
    mkdir /private-source/cargo-auditable; \
    tar -xzf /private-source/cargo-auditable.crate \
        --strip-components=1 --directory=/private-source/cargo-auditable; \
    jq --exit-status \
        '.git.sha1 == "9dc5ff28ff204b73eae91bd96cde4413fdcb70e0"' \
        /private-source/cargo-auditable/.cargo_vcs_info.json >/dev/null \
        || { echo 'cargo-auditable revision mismatch' >&2; exit 1; }; \
    printf '%s  %s\n' \
        3a49de28391ca0e99709a96c64cd8e24f8f96d622f5a8360c2fbd5d8e0d9965e \
        /private-source/cargo-auditable/Cargo.lock | sha256sum --check --status \
        || { echo 'cargo-auditable lockfile mismatch' >&2; exit 1; }; \
    cargo +1.96.1 metadata --locked --no-deps \
        --manifest-path /private-source/cargo-auditable/Cargo.toml \
        --format-version=1 > /private-source/cargo-auditable-metadata.json \
        2> /private-source/cargo-auditable-metadata.log \
        || { echo 'cargo-auditable metadata validation failed' >&2; exit 1; }; \
    jq --exit-status \
        '.packages | length == 1 and \
         .[0].name == "cargo-auditable" and .[0].version == "0.7.5"' \
        /private-source/cargo-auditable-metadata.json >/dev/null \
        || { echo 'cargo-auditable package identity mismatch' >&2; exit 1; }; \
    cargo +1.96.1 install --locked --path /private-source/cargo-auditable \
        --root /usr/local --target "$rust_target" \
        || { echo 'cargo-auditable build failed' >&2; exit 1; }; \
    cargo +1.96.1 install --list --root /usr/local \
        > /private-source/cargo-install-list \
        || { echo 'cargo-auditable installation metadata missing' >&2; exit 1; }; \
    grep -Fxq 'cargo-auditable v0.7.5 (/private-source/cargo-auditable):' \
        /private-source/cargo-install-list \
        || { echo 'cargo-auditable version mismatch' >&2; exit 1; }; \
    grep -Fxq '    cargo-auditable' /private-source/cargo-install-list \
        || { echo 'cargo-auditable installed binary mismatch' >&2; exit 1; }; \
    cargo +1.96.1 auditable --help > /private-source/cargo-auditable-help.log 2>&1 \
        || { echo 'cargo-auditable Cargo delegation failed' >&2; exit 1; }; \
    cd "$source_dir"; \
    cargo +1.96.1 metadata --locked --no-deps --format-version=1 \
        > /private-source/metadata.json 2> /private-source/metadata.log \
        || { echo 'switchyard package metadata validation failed' >&2; exit 1; }; \
    jq --exit-status \
        '.packages | any(.name == "switchyard-server" and .version == "0.1.0")' \
        /private-source/metadata.json >/dev/null \
        || { echo 'switchyard package identity mismatch' >&2; exit 1; }; \
    export CARGO_TARGET_DIR=/cargo-target; \
    cargo +1.96.1 auditable build --locked --release --package switchyard-server \
        > /private-source/cargo-build.log 2>&1 \
        || { echo 'switchyard private source compilation failed' >&2; exit 1; }; \
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
      org.opencontainers.image.revision="6ff491acd7eb1ec239525a1baa015730c3d3564a" \
      org.opencontainers.image.version="0.1.0" \
      org.kew.switchyard.lockfile-sha256="e895886482133481679db318c824919d5502166b8ef3564f87c9f825d0d8e0ab" \
      org.kew.switchyard.package="switchyard-server" \
      org.kew.switchyard.rust-toolchain="1.96.1" \
      org.kew.switchyard.read-only-rootfs="required" \
      org.kew.switchyard.memory-limit="256m" \
      org.kew.switchyard.pids-limit="64" \
      org.kew.switchyard.cpu-limit="1"

USER 65532:65532
WORKDIR /
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/switchyard-server"]
