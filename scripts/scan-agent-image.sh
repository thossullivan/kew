#!/usr/bin/env bash
# Agent-image CVE gate (W1.4 / #1432).
#
# The agent images ARE the containment boundary, and nothing publishes them —
# `docker-agent.yml` has never run and the 2026-08-07 ruling is that it never
# will. So the thing to gate is not a publish: it is the image this checkout
# builds and this machine then dispatches into.
#
# ONE script, called identically by ci.yml's `agent-image-scan` job and by
# scripts/verify.sh, so the two cannot drift — the same stance the docs gate
# (check-docs.sh) and the www gate (npm run gates) already take, and the one
# tests/test_verify_parity.py enforces.
#
# Agent images fail on any CRITICAL/HIGH that HAS A FIX AVAILABLE.
# `--ignore-unfixed` is deliberate and is the difference between their gate
# and a wall: Debian
# bookworm ships ~100 CRITICAL/HIGH with no fix published, so a gate counting
# those would be red on its first run and stay red forever, which is a gate
# nobody reads. Those are triaged in a maintainer-held record and printed
# unfiltered at the
# end of every run so the record can be re-derived rather than trusted. The
# isolation proxy gates every CRITICAL/HIGH, including an unfixed result.
set -euo pipefail

# Trivy needs the repo ROOT as the build context (every Dockerfile COPYs from
# docker/), and this script is called from the root in CI and from kew/ in
# verify.sh. Resolve rather than assume.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# The scanner version IS the gate, so a mismatch REFUSES rather than warning: a
# gate run by an unknown scanner has proven nothing, and "it printed a warning
# and passed" is indistinguishable from a pass (round-1 review, Major 3).
#
# The version is NOT written here. It is read from the pin file kew's
# provisioning recipe installs trivy from, which is also what CI's `Install
# Trivy` step installs — one value, one file, read in three places. A literal
# here is how a host built by kew's own recipe came to carry a trivy this gate
# refuses, which failed every verification at this step AFTER the whole suite
# had passed (#2462).
TRIVY_PIN_FILE="$REPO_ROOT/kew/src/kew/hosts/provision/trivy.pin"
if [ ! -f "$TRIVY_PIN_FILE" ]; then
    echo "scan-agent-image.sh: no scanner pin at $TRIVY_PIN_FILE — this gate is the version that file names, so it cannot run without it." >&2
    exit 1
fi
TRIVY_VERSION="$(awk -F= '$1 == "TRIVY_VERSION" { print $2 }' "$TRIVY_PIN_FILE")"
if [ -z "$TRIVY_VERSION" ]; then
    echo "scan-agent-image.sh: $TRIVY_PIN_FILE defines no TRIVY_VERSION, so the gate has no pinned scanner to be." >&2
    exit 1
fi

# How to install the pinned scanner, said once for every refusal below. A
# Homebrew trivy is a different build of the same version, so its binary is
# not the one trivy.pin pins and this gate refuses it.
TRIVY_INSTALL_HINT="Install the pinned build: download trivy_${TRIVY_VERSION}_<platform>.tar.gz from https://github.com/aquasecurity/trivy/releases/tag/v${TRIVY_VERSION}, check it against the archive SHA-256 in ${TRIVY_PIN_FILE}, and put its trivy first on PATH (a Homebrew trivy is a different binary and is refused). A host kew provisioned installs it from that file; re-run \`kew hosts provision\` on it."

# The SHA-256 of a file, with whichever of sha256sum (Linux) and shasum (macOS)
# this machine has.
file_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum <"$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 <"$1" | awk '{print $1}'
    else
        return 1
    fi
}

# The pinned tools this gate is, checked and nothing else — seconds, no build,
# no image, no database. `kew verify` runs this on the host BEFORE it runs the
# suite (kew/merge_verify.py): a wrong scanner discovered after the suite has
# passed is twelve minutes burned per attempt, every attempt (#2462).
#
# The scanner is identified by its binary's SHA-256, not by what it says its
# version is: `trivy --version` is self-reported, so any binary can answer the
# pin and return clean scans forever (#3258). trivy.pin holds the digest of
# the trivy inside each pinned release archive.
check_pinned_tools() {
    if ! command -v trivy >/dev/null 2>&1; then
        echo "scan-agent-image.sh: trivy not found. $TRIVY_INSTALL_HINT" >&2
        return 1
    fi
    local platform_key expected actual trivy_path
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) platform_key=TRIVY_BINARY_SHA256_LINUX_64BIT ;;
        Linux-aarch64 | Linux-arm64) platform_key=TRIVY_BINARY_SHA256_LINUX_ARM64 ;;
        Darwin-x86_64) platform_key=TRIVY_BINARY_SHA256_MACOS_64BIT ;;
        Darwin-arm64) platform_key=TRIVY_BINARY_SHA256_MACOS_ARM64 ;;
        *)
            echo "scan-agent-image.sh: no trivy build is pinned for $(uname -s)-$(uname -m), so this gate cannot identify a scanner here." >&2
            return 1
            ;;
    esac
    expected="$(awk -F= -v k="$platform_key" '$1 == k { print $2 }' "$TRIVY_PIN_FILE")"
    if [[ ! "$expected" =~ ^[0123456789abcdef]{64}$ ]]; then
        echo "scan-agent-image.sh: $TRIVY_PIN_FILE pins no $platform_key, so the gate has no scanner binary to be." >&2
        return 1
    fi
    trivy_path="$(command -v trivy)"
    if ! actual="$(file_sha256 "$trivy_path")"; then
        echo "scan-agent-image.sh: neither sha256sum nor shasum is available, so $trivy_path cannot be identified. Refusing to gate with an unidentified scanner." >&2
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        echo "scan-agent-image.sh: $trivy_path is not the pinned trivy $TRIVY_VERSION binary: its SHA-256 is ${actual:-<unreadable>}, and $TRIVY_PIN_FILE pins $expected for this platform. Whatever version it reports, a different binary is a different gate, so this refuses. $TRIVY_INSTALL_HINT" >&2
        return 1
    fi
    # Deliberately not `local`: the certification line printed after the
    # database check names the scanner this gate ran as, and it reads this.
    found="$(trivy --version | awk '/^Version:/ {print $2}')"
    if [ "$found" != "$TRIVY_VERSION" ]; then
        echo "scan-agent-image.sh: trivy ${found:-<unknown>} is not the pinned $TRIVY_VERSION — a different scanner version is a different gate, so this refuses rather than gating with an unknown one. $TRIVY_INSTALL_HINT" >&2
        return 1
    fi
    printf 'scan-agent-image.sh: trivy %s, the pinned scanner.\n' "$found"
}

# Placed here, above everything else this script does: nothing before it builds
# an image, starts a container or touches the network, so the preflight really
# is the seconds it claims to be.
if [ "$#" -eq 1 ] && [ "$1" = "--check-tools" ]; then
    check_pinned_tools
    exit 0
fi

# The vulnerability database must be present and recent. A scanner with an
# empty or ancient DB reports no findings on a vulnerable image, and the
# package-count floor below cannot see that — packages are inventoried from the
# image, advisories come from the DB (round-1 review, Major 4). Trivy
# self-updates every 24h, so a fortnight is slack, not a schedule.
TRIVY_DB_MAX_AGE_DAYS=14

# Per-ecosystem inventory floors. Debian slim alone is ~100 packages and the
# Node tree ~150, so these separate "scanned" from "did not
# scan" without pinning numbers that drift. They are deliberately per-ecosystem
# AND per-type: a whole-image total let one ecosystem's absence hide behind the
# other's size, and a bare lang-pkgs count let pip packages stand in for npm.
MIN_OS_PACKAGES=50
MIN_NPM_PACKAGES=50
MIN_SWITCHYARD_OS_PACKAGES=50
MIN_SWITCHYARD_RUST_PACKAGES=50

# Reviewed private source binding. The archive is never copied into the build
# context. This host check runs before every build, including a cache hit where
# BuildKit would not re-read secret contents.
SWITCHYARD_SOURCE_ARCHIVE_SHA256="a53aac972293ceef2ac2f5a84da92e022cc1d4a903521761cc90f18655cfb31e"
SWITCHYARD_SOURCE_ARCHIVE_SIZE=7454720

# Every agent image, not just the reference one: agent-opencode.Dockerfile is
# what the `simple` routing tier dispatches into, so leaving it unscanned would
# leave the busiest lane dark, and agent-codex.Dockerfile is the image the
# OpenShell pilot launches. Tags are scan-only and never dispatched.
# The tag the built images are given. Overridable so an operator rebuilding
# for a re-pin can name the images directly (KEW_SCAN_TAG=docker-<digest12>)
# instead of retagging afterwards. Retagging from a shared mutable tag is how
# a re-pin once recorded the wrong images: a concurrent verify run (kew review
# runs the repo gate in its own checkout) rebuilt :ci-scan from another tree
# between this script's scan and the operator's docker tag. The scan itself is
# bound to the --iidfile ID and was never fooled; only the retag was. Rule:
# retag from the ID this script prints, never from the tag.
SCAN_TAG="${KEW_SCAN_TAG:-ci-scan}"
# Only the two forms this repo uses. Docker parses a slash in a tag as a
# registry host (kew-agent:5000/x becomes registry kew-agent:5000), so an
# arbitrary value would build under a name nothing here expects (round-5
# review).
# Spelled out rather than [0-9a-f]: a POSIX range is collated by the current
# locale, and some locales sort uppercase letters inside a-f (round-6 review).
if [[ ! "$SCAN_TAG" =~ ^(ci-scan|docker-[0123456789abcdef]{12})$ ]]; then
    echo "scan-agent-image.sh: KEW_SCAN_TAG must be ci-scan or docker-<12 hex>, got '${SCAN_TAG}'. Refusing to build under an unexpected image name." >&2
    exit 1
fi
IMAGES=(
    "agent|docker/agent.Dockerfile|kew-agent:${SCAN_TAG}"
    "agent|docker/agent-opencode.Dockerfile|kew-agent-opencode:${SCAN_TAG}"
    "agent|docker/agent-codex.Dockerfile|kew-agent-codex:${SCAN_TAG}"
    "switchyard|docker/switchyard.Dockerfile|kew-switchyard:${SCAN_TAG}"
)
# Derived from the array, never written out. The success line below said "both
# agent images" while the array held three, so the gate's own certification
# understated what it had gated — and a certification nobody can trust to be
# arithmetic is the one thing a gate must not print (#2317 review). Counted
# again as the loop certifies each one, and the two are compared before
# anything is certified, so a `break` cannot shrink the claim silently.
AGENT_IMAGES_DECLARED=0
for entry in "${IMAGES[@]}"; do
    if [ "${entry%%|*}" != switchyard ]; then
        AGENT_IMAGES_DECLARED=$((AGENT_IMAGES_DECLARED + 1))
    fi
done
agent_images_gated=0
# Images whose gate found a finding. The gate is a verdict on every image, so a
# finding in one does not stop the scan of the next: the nightly files an issue
# per CVE across all of them (#3195). The run fails at the end instead.
gate_findings=0
gate_findings_in=""
# trivy exits this, and only this, when the gate finds something; any other
# non-zero exit is a scan that could not run — a scanner error, never a
# finding — and is counted and reported apart (#3195).
GATE_FINDINGS_EXIT=3
gate_errors=0
gate_errors_in=""
record_gate() {
    case "$1" in
        0) ;;
        "$GATE_FINDINGS_EXIT")
            gate_findings=$((gate_findings + 1))
            gate_findings_in="$gate_findings_in $tag"
            echo "scan-agent-image.sh: gate findings in $tag" >&2
            ;;
        *)
            gate_errors=$((gate_errors + 1))
            gate_errors_in="$gate_errors_in $tag (trivy exited $1 without a verdict)"
            echo "scan-agent-image.sh: gate error in $tag: trivy exited $1 without a verdict" >&2
            ;;
    esac
}
# An image whose build or inventory failed never reaches its gate. That is
# recorded with the image and why, in the same `gate error in <tag>:` line a
# scanner error prints, and the scan goes on to the next image: one broken
# image must not leave the others ungated. The run fails at the end, naming
# every one (#3311). Counted apart from the gated images so the closing check
# can still tell a sweep that visited every image from one that stopped.
agent_images_failed=0
image_failed() {
    gate_errors=$((gate_errors + 1))
    gate_errors_in="$gate_errors_in $tag ($1)"
    echo "scan-agent-image.sh: gate error in $tag: $1" >&2
    if [ "$policy" != switchyard ]; then
        agent_images_failed=$((agent_images_failed + 1))
    fi
    if [ -n "$iidfile" ]; then
        rm -f "$iidfile"
        iidfile=""
    fi
}

private_switchyard_source() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "scan-agent-image.sh: Switchyard source archive validation failed: isolated Python unavailable." >&2
        return 1
    fi
    local -a source_command=(python3)
    # Startup's caller owns this PID. Replace Bash so cancellation reaches the producer.
    if [ "$1" = build-no-cache ]; then source_command=(exec python3); fi
    "${source_command[@]}" -I -B - "$REPO_ROOT" "$SWITCHYARD_SOURCE_ARCHIVE_SHA256" \
        "$SWITCHYARD_SOURCE_ARCHIVE_SIZE" "$SCAN_TAG" "$1" <<'PY'
# KEW_PRIVATE_SOURCE_PYTHON_BEGIN
"""Shared isolated source validation and bounded private BuildKit transport."""

import contextlib
import hashlib
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time

CHUNK_SIZE = 491520
CHUNK_COUNT = 16
DEADLINE_SECONDS = 1650
TERMINATION_GRACE_SECONDS = 10
DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class Refusal(Exception):
    pass


def refuse(category="path or descriptor operation failed"):
    raise Refusal(category)


def fingerprint(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def directory_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def components_of(path):
    if not isinstance(path, str) or not path.startswith("/"):
        refuse("path is not absolute and unambiguous")
    if path == "/":
        return []
    values = path.split("/")[1:]
    if any(value in ("", ".", "..") for value in values) or any(
        ord(c) < 32 for c in path
    ):
        refuse("path is not absolute and unambiguous")
    return values


def require_descriptor_apis():
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        refuse("descriptor APIs unavailable")
    if any(
        fn not in os.supports_dir_fd
        for fn in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir)
    ):
        refuse("descriptor APIs unavailable")
    if os.stat not in os.supports_follow_symlinks or os.listdir not in os.supports_fd:
        refuse("descriptor APIs unavailable")


class Directory:
    """Retain every no-follow pathname component until the operation finishes."""

    def __init__(self, path):
        self.path = path
        self.names = components_of(path)
        self.fds = []
        self.identities = []
        try:
            self.fds.append(os.open("/", DIRECTORY_FLAGS))
            self.identities.append(directory_identity(os.fstat(self.fds[-1])))
            for name in self.names:
                before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    refuse("linked or invalid parent")
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=self.fd)
                self.fds.append(child)
                opened = os.fstat(child)
                if directory_identity(before) != directory_identity(opened):
                    refuse("changed during validation")
                self.identities.append(directory_identity(opened))
            self.check()
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        return self.fds[-1]

    def check(self):
        for index, fd in enumerate(self.fds):
            if directory_identity(os.fstat(fd)) != self.identities[index]:
                refuse("changed during validation")
            if (
                index
                and directory_identity(
                    os.stat(
                        self.names[index - 1],
                        dir_fd=self.fds[index - 1],
                        follow_symlinks=False,
                    )
                )
                != self.identities[index]
            ):
                refuse("changed during validation")

    def close(self):
        while self.fds:
            os.close(self.fds.pop())


class Source:
    def __init__(self, path, repo, digest, size):
        self.fd = None
        self.parent = None
        self.repo, self.digest, self.size = repo, digest, size
        values = components_of(path)
        if not values:
            refuse("input is missing")
        self.leaf = values[-1]
        if os.path.commonpath((path, repo.path)) == repo.path:
            refuse("checkout containment")
        try:
            self.parent = Directory("/" + "/".join(values[:-1]))
            if repo.identities[-1][:2] in {item[:2] for item in self.parent.identities}:
                refuse("checkout containment")
            parent = os.fstat(self.parent.fd)
            if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                refuse("immediate parent is not owned and private")
            before = os.stat(self.leaf, dir_fd=self.parent.fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                refuse("linked or special leaf")
            self.fd = os.open(self.leaf, FILE_FLAGS, dir_fd=self.parent.fd)
            value = os.fstat(self.fd)
            if fingerprint(before) != fingerprint(value):
                refuse("changed during validation")
            if value.st_nlink != 1:
                refuse("hard-linked leaf")
            if (
                value.st_uid != os.geteuid()
                or not value.st_mode & stat.S_IRUSR
                or value.st_mode & 0o077
            ):
                refuse("leaf is not owned, readable, and private")
            if value.st_size != size:
                refuse("length mismatch")
            self.identity = fingerprint(value)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        self.repo.check()
        self.parent.check()
        if (
            fingerprint(os.fstat(self.fd)) != self.identity
            or fingerprint(
                os.stat(self.leaf, dir_fd=self.parent.fd, follow_symlinks=False)
            )
            != self.identity
        ):
            refuse("changed during validation")

    def stream(self, consume=None):
        self.check()
        os.lseek(self.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = self.size
        for index in range(CHUNK_COUNT):
            wanted = min(CHUNK_SIZE, remaining)
            data = bytearray()
            while len(data) < wanted:
                block = os.read(self.fd, wanted - len(data))
                if not block:
                    refuse("length mismatch")
                data.extend(block)
            digest.update(data)
            remaining -= len(data)
            if consume is not None:
                consume(index, data)
        if remaining or os.read(self.fd, 1):
            refuse("length mismatch")
        self.check()
        if digest.hexdigest() != self.digest:
            refuse("digest mismatch")
        return self.identity

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent is not None:
            self.parent.close()


def validate_source(source):
    first = source.stream()
    second = source.stream()
    if first != second:
        refuse("changed during validation")


class Stage:
    def __init__(self, repo, temporary):
        self.parent = None
        self.fd = None
        self.entries = {}
        self.repo = repo
        created = False
        try:
            self.parent = Directory(temporary)
            if "," in temporary or repo.identities[-1][:2] in {
                item[:2] for item in self.parent.identities
            }:
                refuse("checkout containment")
            self.parent.check()
            for _ in range(128):
                self.name = "kew-switchyard-chunks." + secrets.token_hex(12)
                try:
                    os.mkdir(self.name, 0o700, dir_fd=self.parent.fd)
                    created = True
                    break
                except FileExistsError:
                    continue
            else:
                refuse()
            before = os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
            self.fd = os.open(self.name, DIRECTORY_FLAGS, dir_fd=self.parent.fd)
            opened = os.fstat(self.fd)
            if fingerprint(before) != fingerprint(opened):
                refuse("changed during validation")
            self.identity = directory_identity(opened)
            if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
                refuse()
            self.path = temporary.rstrip("/") + "/" + self.name
            self.check()
        except BaseException:
            # No recursive cleanup occurs when initial directory ownership is uncertain.
            self.close()
            if created:
                refuse("cleanup unconfirmed")
            raise

    def check(self):
        self.repo.check()
        self.parent.check()
        if (
            directory_identity(os.fstat(self.fd)) != self.identity
            or directory_identity(
                os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
            )
            != self.identity
        ):
            refuse("changed during validation")

    def check_entry(self, name):
        fd, created, sealed = self.entries[name]
        opened = os.fstat(fd)
        path = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if (
            fingerprint(opened) != fingerprint(path)
            or opened.st_dev != created.st_dev
            or opened.st_ino != created.st_ino
            or opened.st_uid != created.st_uid
            or opened.st_gid != created.st_gid
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_mode & 0o077
        ):
            refuse("changed during validation")
        if sealed is not None and fingerprint(opened) != sealed:
            refuse("changed during validation")

    def check_entries(self):
        self.check()
        if set(os.listdir(self.fd)) != set(self.entries):
            refuse("changed during validation")
        for name in self.entries:
            self.check_entry(name)

    def write_chunk(self, index, data):
        self.check_entries()
        name = f"part-{index:02d}"
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            self.entries[name] = (fd, os.fstat(fd), None)
        except BaseException:
            os.close(fd)
            raise
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                refuse()
            view = view[count:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        self.entries[name] = (fd, self.entries[name][1], fingerprint(os.fstat(fd)))
        self.check_entry(name)

    def verify_chunks(self, source):
        self.check_entries()
        digest = hashlib.sha256()
        for index in range(CHUNK_COUNT):
            name = f"part-{index:02d}"
            fd = self.entries[name][0]
            wanted = min(CHUNK_SIZE, max(0, source.size - index * CHUNK_SIZE))
            if os.fstat(fd).st_size != wanted:
                refuse("length mismatch")
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = wanted
            while remaining:
                block = os.read(fd, remaining)
                if not block:
                    refuse("length mismatch")
                remaining -= len(block)
                digest.update(block)
            if os.read(fd, 1):
                refuse("length mismatch")
            self.check_entry(name)
        if digest.hexdigest() != source.digest:
            refuse("digest mismatch")
        source.check()

    def adopt_iid(self, required=False):
        self.check()
        if "image.iid" in self.entries:
            self.check_entry("image.iid")
            return
        try:
            before = os.stat("image.iid", dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            if required:
                refuse("build failed")
            return
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or not before.st_mode & stat.S_IRUSR
            or before.st_size > 4096
        ):
            refuse("build failed")
        fd = os.open("image.iid", FILE_FLAGS, dir_fd=self.fd)
        self.entries["image.iid"] = (fd, before, fingerprint(before))
        self.check_entry("image.iid")

    def image_id(self):
        self.adopt_iid(required=True)
        fd = self.entries["image.iid"][0]
        if os.fstat(fd).st_size not in (71, 72):
            refuse("build failed")
        os.lseek(fd, 0, os.SEEK_SET)
        value = os.read(fd, 73)
        if not re.fullmatch(rb"sha256:[0-9a-f]{64}\n?", value) or os.read(fd, 1):
            refuse("build failed")
        self.check_entry("image.iid")
        return value.rstrip(b"\n").decode("ascii")

    def cleanup(self):
        # Check the complete set first. Retain uncertain entries.
        self.check_entries()
        for name in list(self.entries):
            self.check()
            self.check_entry(name)
            os.unlink(name, dir_fd=self.fd)
            os.close(self.entries.pop(name)[0])
        self.check_entries()
        os.rmdir(self.name, dir_fd=self.parent.fd)
        try:
            os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        refuse("cleanup unconfirmed")

    def close(self):
        for fd, _created, _sealed in self.entries.values():
            os.close(fd)
        self.entries.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent is not None:
            self.parent.close()


def group_exists(process):
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(process):
    if process is None:
        return
    for kind in (signal.SIGTERM, signal.SIGKILL):
        status = process.poll()
        if not group_exists(process):
            if status is None:
                process.wait(timeout=TERMINATION_GRACE_SECONDS)
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, kind)
        limit = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < limit:
            status = process.poll()
            if not group_exists(process):
                if status is None:
                    process.wait(timeout=TERMINATION_GRACE_SECONDS)
                return
            time.sleep(0.02)
    refuse("cleanup unconfirmed")


def main():
    repo = source = stage = process = None
    image_id = None
    failure = None
    launching = cancelled = False

    def interrupt(_signum, _frame):
        nonlocal cancelled
        cancelled = True
        if not launching:
            refuse("interrupted")

    old_handlers = {}
    previous_umask = os.umask(0o077)
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        for kind in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM):
            old_handlers[kind] = signal.signal(kind, interrupt)
        signal.alarm(DEADLINE_SECONDS)
        require_descriptor_apis()
        if len(sys.argv) != 6:
            refuse("reviewed binding is invalid")
        repo_path, digest, size, tag, action = sys.argv[1:]
        size = int(size)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not 0 < size <= CHUNK_SIZE * CHUNK_COUNT
            or not re.fullmatch(r"ci-scan|docker-[0-9a-f]{12}", tag)
            or action not in ("validate", "build", "build-no-cache")
        ):
            refuse("reviewed binding is invalid")
        archive = os.environ.get("KEW_SWITCHYARD_SOURCE_ARCHIVE", "")
        if not archive:
            refuse("input is missing")
        repo = Directory(repo_path)
        source = Source(archive, repo, digest, size)
        validate_source(source)
        if action != "validate":
            temporary = os.environ.get("TMPDIR") or (
                "/private/tmp" if sys.platform == "darwin" else "/tmp"
            )
            temporary = temporary.rstrip("/") or "/"
            components_of(temporary)
            # Only the staging-root policy resolves platform aliases such as macOS /var.
            # The source path never resolves links. Docker receives the canonical path.
            stage = Stage(repo, os.path.realpath(temporary))
            source.stream(stage.write_chunk)
            stage.verify_chunks(source)
            argv = [
                "docker",
                "build",
                "-f",
                "docker/switchyard.Dockerfile",
                "-t",
                "kew-switchyard:" + tag,
                "--iidfile",
                stage.path + "/image.iid",
            ]
            if action == "build-no-cache":
                argv.append("--no-cache")
            for index in range(CHUNK_COUNT):
                argv.extend(
                    (
                        "--secret",
                        f"id=switchyard_source_archive_{index:02d},src={stage.path}/part-{index:02d}",
                    )
                )
            argv.append(".")
            stage.check_entries()
            source.check()
            environment = dict(os.environ)
            environment.pop("KEW_SWITCHYARD_SOURCE_ARCHIVE", None)
            environment["DOCKER_BUILDKIT"] = "1"
            # Latch cancellation until the newly created process has an owned handle.
            launching = True
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=repo.path,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                launching = False
            if cancelled:
                refuse("interrupted")
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            source.check()
            stage.adopt_iid(required=not status)
            stage.verify_chunks(source)
            if status:
                refuse("build failed")
            image_id = stage.image_id()
    except subprocess.TimeoutExpired:
        failure = Refusal("build timed out")
    except BaseException as error:
        failure = error
    finally:
        signal.alarm(0)
        for kind in old_handlers:
            signal.signal(kind, signal.SIG_IGN)
        try:
            terminate(process)
            if stage is not None:
                stage.adopt_iid()
                stage.cleanup()
        except BaseException:
            failure = Refusal("cleanup unconfirmed")
        if source is not None:
            try:
                source.check()
            except BaseException as error:
                if failure is None:
                    failure = error
        for resource in (stage, source, repo):
            if resource is not None:
                try:
                    resource.close()
                except BaseException:
                    failure = Refusal("cleanup unconfirmed")
        os.umask(previous_umask)
        for kind, handler in old_handlers.items():
            signal.signal(kind, handler)
    if failure is not None:
        categories = {
            "input is missing",
            "descriptor APIs unavailable",
            "path is not absolute and unambiguous",
            "linked or invalid parent",
            "linked or special leaf",
            "changed during validation",
            "immediate parent is not owned and private",
            "hard-linked leaf",
            "leaf is not owned, readable, and private",
            "length mismatch",
            "digest mismatch",
            "checkout containment",
            "reviewed binding is invalid",
            "build failed",
            "build timed out",
            "interrupted",
            "cleanup unconfirmed",
        }
        category = (
            failure.args[0] if isinstance(failure, Refusal) and failure.args else None
        )
        if category not in categories:
            category = "path or descriptor operation failed"
        print(
            "scan-agent-image.sh: Switchyard source archive validation failed: "
            + category
            + ".",
            file=sys.stderr,
        )
        return 1
    if image_id is not None:
        print(image_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# KEW_PRIVATE_SOURCE_PYTHON_END
PY
}

validate_switchyard_source() {
    private_switchyard_source validate
}

build_switchyard_image() {
    private_switchyard_source "$1"
}

# Opt-in switch. The Switchyard layer runs only when KEW_SWITCHYARD_IMAGE is
# "1": the pinned private source input exists on the private runner alone, so
# the default — every machine without it, including the verify VM, a hosted
# runner and a contributor's laptop — skips that layer, and only that layer,
# with SWITCHYARD_IMAGE_SKIP_REASON. Every agent image is still built and
# gated, and the default never touches the private source input: it does not
# even validate the archive. ci.yml sets "1" exactly when the repository is
# private.
# Any other value refuses, so a typo cannot opt in or out silently.
# docs/operations.md ("Public repository runner rule") records the switch.
SWITCHYARD_IMAGE_SKIP_REASON="the pinned Switchyard input includes unreleased upstream patches; it builds on the private runner until they are public"
case "${KEW_SWITCHYARD_IMAGE:-}" in
    "") run_switchyard_image=0 ;;
    1) run_switchyard_image=1 ;;
    *)
        echo "scan-agent-image.sh: KEW_SWITCHYARD_IMAGE must be unset, empty or 1." >&2
        exit 1
        ;;
esac
if [ "$run_switchyard_image" = 0 ] && [ "$#" -ne 0 ]; then
    echo "scan-agent-image.sh: the Switchyard image is skipped unless KEW_SWITCHYARD_IMAGE=1 ($SWITCHYARD_IMAGE_SKIP_REASON), so a Switchyard-only action cannot run." >&2
    exit 1
fi

# The build-only action shares all validation, staging, and cleanup logic.
# It emits only the immutable IID after confirmed cleanup. It never skips cache validation.
if [ "$#" -eq 1 ] && [ "$1" = "--build-switchyard-only" ]; then
    build_switchyard_image build-no-cache
    exit 0
fi
if [ "$run_switchyard_image" = 1 ]; then
    validate_switchyard_source
fi
if [ "${1:-}" = "--validate-switchyard-source" ] && [ "$#" -eq 1 ]; then
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "scan-agent-image.sh: unsupported argument." >&2
    exit 1
fi

cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "scan-agent-image.sh: no reachable container runtime — the agent image cannot be built, so the CVE gate cannot run. Start Docker/OrbStack and re-run." >&2
    exit 1
fi

# The same check `--check-tools` runs on its own, run again here: the gate is
# never reached without it, whether or not anything ran the preflight first.
check_pinned_tools

# Bootstrap the database BEFORE inspecting its metadata. On a clean runner
# `trivy version --format json` reports no VulnerabilityDB at all — there is
# nothing to report until the first download — so checking the metadata first
# refused every fresh machine (round-2 review, Major 2). This also self-heals a
# cache that has gone stale. It is the only network operation the gate performs.
echo "==> ensuring the vulnerability database is present and current"
if ! trivy image --skip-version-check --download-db-only; then
    echo "scan-agent-image.sh: could not download trivy's vulnerability database. The gate cannot run without it — a scanner with no advisories reports no findings on any image." >&2
    exit 1
fi

db_age_seconds="$(trivy version --format json | python3 -c '
import datetime, json, sys

# Use datetime.timezone.utc, never the datetime.UTC alias. This runs under
# whatever python3 the HOST provides, and that alias only exists from 3.11.
# The CI runner system interpreter is older than the repo uv-managed one, so
# datetime.UTC crashed there after passing every local run and five review
# rounds — nothing else in this repo executes under the host interpreter.
#
# Also: no apostrophes anywhere in this block. It sits inside a single-quoted
# python3 -c argument, and one contraction closes the shell string and
# truncates the program. That is how this very comment first broke it.

meta = json.load(sys.stdin).get("VulnerabilityDB") or {}
stamp = meta.get("UpdatedAt")
if not stamp:
    print("missing")
    raise SystemExit
stamp = stamp.replace("Z", "+00:00")
# Trivy emits nanoseconds; datetime handles at most microseconds. Split on the
# LAST +/- so a negative UTC offset is not mistaken for part of the fraction.
if "." in stamp:
    head, _, tail = stamp.partition(".")
    for sign in ("+", "-"):
        idx = tail.rfind(sign)
        if idx > 0:
            stamp = f"{head}.{tail[:idx][:6]}{tail[idx:]}"
            break
    else:
        stamp = f"{head}.{tail[:6]}"
try:
    updated = datetime.datetime.fromisoformat(stamp)
except ValueError:
    print("missing")
    raise SystemExit
if updated.tzinfo is None:
    updated = updated.replace(tzinfo=datetime.timezone.utc)
# Signed seconds, but the FUTURE test happens here in float space. int() of a
# -0.8s age is 0, so a stamp less than a second in the future looked like the
# present (round-3 review, Minor 5). Report the sentinel instead and let the
# shell refuse on it.
age = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
print("future" if age < 0 else int(age))
')"
if [ "$db_age_seconds" = "missing" ]; then
    echo "scan-agent-image.sh: trivy reports no vulnerability database even after a download attempt — it would find nothing in any image, and a gate that finds nothing passes everything." >&2
    exit 1
fi
if [ "$db_age_seconds" = "future" ]; then
    echo "scan-agent-image.sh: trivy's vulnerability database is timestamped in the future. That is a broken clock or a forged timestamp, not a fresh database, and it must not be read as freshness." >&2
    exit 1
fi
if [ "$db_age_seconds" -gt $((TRIVY_DB_MAX_AGE_DAYS * 86400)) ]; then
    echo "scan-agent-image.sh: trivy's vulnerability database is $((db_age_seconds / 86400)) days old (limit ${TRIVY_DB_MAX_AGE_DAYS}) and did not refresh — it cannot know about anything published since." >&2
    exit 1
fi
echo "trivy $found, vulnerability DB $((db_age_seconds / 3600))h old"

# The shared tag is a name, not a hold (#3157). CI and a local merge verify
# share one Docker daemon on the operator Mac, and a second run there rebuilds
# the same :ci-scan tags. That daemon (OrbStack) uses the containerd image
# store, which keeps no dangling images: the moment the other run moves a tag,
# this run's image loses its last name and is deleted, with no prune involved,
# and trivy, handed the --iidfile ID a second earlier, fails "No such image" —
# reported as malformed inventory JSON. The classic store keeps an untagged
# image until a prune, which only delays the same loss. So each build's ID also
# gets a tag only this run uses, held until the script exits, and that holds
# the image under both stores. It is minted here from the PID and 64 random
# bits and never read from the environment, so KEW_SCAN_TAG still admits only
# its two forms and no caller can name an image the EXIT trap below will untag.
# It cannot equal either shared form, and the trap removes only references
# ending in it: never a shared tag, never an ID.
run_tag="scan-run-$$-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
if [[ ! "$run_tag" =~ ^scan-run-[0123456789]+-[0123456789abcdef]{16}$ ]]; then
    echo "scan-agent-image.sh: could not mint this run's image reference (got '${run_tag}'). Refusing to scan an image nothing holds." >&2
    exit 1
fi
run_refs=()
iidfile=""
release_run_refs() {
    local ref
    if [ -n "$iidfile" ]; then rm -f "$iidfile"; fi
    # The guarded expansion: bash 3.2 reads an empty array as unbound under -u.
    for ref in ${run_refs[@]+"${run_refs[@]}"}; do
        if [[ "$ref" == *":${run_tag}" ]] && ! docker rmi "$ref" >/dev/null; then
            echo "scan-agent-image.sh: could not remove this run's reference $ref; remove it with: docker rmi $ref" >&2
        fi
    done
}
trap release_run_refs EXIT

# A run killed by SIGKILL never reaches that trap (the local verify kills
# timeout orphans by PID, #3154), and every tag it leaked pins a full image.
# So before the first build, remove the per-run references whose run is gone:
# only in the four repositories this script builds, only a tag of exactly the
# minted form, and only when its PID is not running. A live PID's reference is
# another run's hold and stays, whatever else is true of it; so does any other
# tag. `ps -p` backs `kill -0`, which also fails on a live process another user
# owns. A reused PID keeps a stale reference, the safe direction.
remove_stale_run_refs() {
    local entry repository refs ref pid
    for entry in "${IMAGES[@]}"; do
        repository="${entry##*|}"
        repository="${repository%:*}"
        if ! refs="$(docker images --format '{{.Repository}}:{{.Tag}}' "$repository")"; then
            echo "scan-agent-image.sh: could not list $repository to remove references left by killed runs; continuing." >&2
            continue
        fi
        while IFS= read -r ref; do
            [ "${ref%%:*}" = "$repository" ] || continue
            [[ "${ref#*:}" =~ ^scan-run-([0123456789]+)-[0123456789abcdef]{16}$ ]] || continue
            pid="${BASH_REMATCH[1]}"
            if kill -0 "$pid" 2>/dev/null || ps -p "$pid" >/dev/null 2>&1; then
                continue
            fi
            if docker rmi "$ref" >/dev/null; then
                echo "==> removed $ref: left by run $pid, which is no longer running"
            else
                echo "scan-agent-image.sh: could not remove $ref, left by run $pid; continuing." >&2
            fi
        done <<< "$refs"
    done
}
remove_stale_run_refs

# Every agent image this gate builds carries the digest of the inputs it was
# built from, as the label `kew sandbox build` writes: a launch reads it back
# before trusting a docker-<digest> tag (#3160). Computed by the one
# implementation kew itself uses, a standard-library module run here with the
# host's python3, so the two builders cannot label the same inputs differently.
if ! inputs_label="$(python3 -I -B "$REPO_ROOT/kew/src/kew/image_inputs.py" "$REPO_ROOT")"; then
    echo "scan-agent-image.sh: could not compute the agent image inputs digest. Refusing to build images that cannot say what they were built from." >&2
    exit 1
fi

for entry in "${IMAGES[@]}"; do
    policy="${entry%%|*}"
    remainder="${entry#*|}"
    dockerfile="${remainder%%|*}"
    tag="${remainder#*|}"

    if [ "$policy" = "switchyard" ] && [ "$run_switchyard_image" = 0 ]; then
        echo "==> skipping $dockerfile -> $tag: $SWITCHYARD_IMAGE_SKIP_REASON"
        continue
    fi
    if [ "$policy" = "switchyard" ]; then
        validate_switchyard_source
    fi

    # --iidfile binds every step below to the image this build PRODUCED, not to
    # whatever the tag happens to point at by the time trivy reads it. Scanning
    # by tag let a concurrent retag substitute a clean image for a vulnerable
    # one between build and scan (round-1 review, Major 4). The tag is still
    # applied, for a human reading `docker images`.
    echo "==> building $dockerfile -> $tag"
    if [ "$policy" = "switchyard" ]; then
        image_id="$(build_switchyard_image build)"
    else
        iidfile="$(mktemp)"
        build_args=(build --no-cache -f "$dockerfile" -t "$tag"
            --label "$inputs_label" --iidfile "$iidfile" .)
        if ! DOCKER_BUILDKIT=1 docker "${build_args[@]}"; then
            image_failed "docker build of $dockerfile failed"
            continue
        fi
        image_id="$(cat "$iidfile")"
    fi
    # mktemp creates the file empty and `cat` of an empty file exits 0, so a
    # builder that returns success without writing an ID would leave every
    # trivy call below with an empty target — the opposite of the immutable-ID
    # binding this exists to provide (round-2 review, Minor 7).
    # A FULL digest, not a prefix. `case` globbing has no counted repetition, so
    # `sha256:[0-9a-f]*` accepted `sha256:a` followed by anything at all —
    # `sha256:abc/../../etc` passed. Validating one character and calling the
    # result an immutable ID is the same shape of defect as the ones this gate
    # exists to catch.
    if [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "scan-agent-image.sh: the build of $dockerfile did not report a full image digest (--iidfile held ${image_id:-<empty>}). Refusing rather than scanning an unidentified target." >&2
        exit 1
    fi
    echo "    built $image_id"
    # By the ID, never the tag: the tag may already name another run's build.
    run_ref="${tag%:*}:${run_tag}"
    if ! docker tag "$image_id" "$run_ref"; then
        echo "scan-agent-image.sh: could not hold $image_id as $run_ref; docker's error is just above. One possible cause: another run on this Docker daemon rebuilt $tag, and the image lost its last name before this run could tag it (#3157). Refusing rather than scanning an image nothing holds." >&2
        exit 1
    fi
    run_refs+=("$run_ref")

    # A scanner that reads nothing reports nothing, and "no findings" is then
    # indistinguishable from "clean".
    #
    # Counting TOTAL packages was not enough. Trivy analyses each ecosystem
    # separately, so a run that inventoried Debian and silently analysed no
    # Node packages at all cleared a whole-image floor while proving nothing
    # about npm — and every fixable CRITICAL/HIGH this gate was built to catch
    # is an npm finding (round-4 review, Major 3). Both ecosystems must be
    # present, each above its own floor.
    echo "==> proving the scan reads $image_id"
    if ! counts="$(trivy image --skip-version-check --scanners vuln --list-all-pkgs \
        --format json "$image_id" \
        | python3 -I -B -c '
import json, sys

def refuse(detail):
    print(f"scan-agent-image.sh: {detail}.", file=sys.stderr)
    raise SystemExit(1)

try:
    data = json.load(sys.stdin)
except (TypeError, ValueError):
    refuse("trivy returned malformed inventory JSON")
if not isinstance(data, dict):
    refuse("trivy returned malformed inventory JSON")
results = data.get("Results") or []
if not isinstance(results, list):
    refuse("trivy returned malformed inventory JSON")
os_pkgs = npm_pkgs = rust_pkgs = 0
for result in results:
    if not isinstance(result, dict):
        continue
    packages = result.get("Packages") or []
    n = len(packages) if isinstance(packages, list) else 0
    if result.get("Class") == "os-pkgs":
        os_pkgs += n
    # lang-pkgs is EVERY language ecosystem, not Node, and the type string is
    # "node-pkg" -- not "npm". This image reports 152 debian, 151 node-pkg and
    # 1066 rustbinary packages (uv and uvx), so the old whole-class count of
    # 1217 was five-sixths Rust: a TOTAL failure of the Node analysis would
    # still have cleared a floor of 50. That was a live false pass, not a
    # hypothetical one, and every fixable CRITICAL/HIGH in these images is a
    # Node finding.
    elif result.get("Class") == "lang-pkgs" and result.get("Type") == "node-pkg":
        npm_pkgs += n

if sys.argv[1] == "switchyard":
    matches = [
        result
        for result in results
        if isinstance(result, dict)
        and result.get("Target") == "usr/local/bin/switchyard-server"
        and result.get("Class") == "lang-pkgs"
        and result.get("Type") == "rustbinary"
    ]
    if len(matches) != 1:
        refuse("Switchyard Rust inventory result is missing or duplicated")
    packages = matches[0].get("Packages")
    if not isinstance(packages, list):
        refuse("Switchyard Rust inventory contains malformed package identities")
    package_ids = []
    roots = []
    for package in packages:
        if not isinstance(package, dict):
            refuse("Switchyard Rust inventory contains malformed package identities")
        package_id = package.get("ID")
        name = package.get("Name")
        version = package.get("Version")
        if (
            not all(
                isinstance(value, str) and value and value.strip() == value
                for value in (package_id, name, version)
            )
            or package_id != f"{name}@{version}"
        ):
            refuse("Switchyard Rust inventory contains malformed package identities")
        package_ids.append(package_id)
        if package.get("Relationship") == "root":
            roots.append(package)
    if len(package_ids) != len(set(package_ids)):
        refuse("Switchyard Rust inventory contains duplicate package IDs")
    expected_root = {
        "ID": "switchyard-server@0.1.0",
        "Name": "switchyard-server",
        "Version": "0.1.0",
        "Relationship": "root",
    }
    if len(roots) != 1 or any(
        roots[0].get(key) != value for key, value in expected_root.items()
    ):
        refuse("Switchyard Rust inventory root package mismatch")
    rust_pkgs = len(package_ids)
print(os_pkgs, npm_pkgs, rust_pkgs)
' "$policy")"; then
        image_failed "trivy could not inventory $image_id"
        continue
    fi
    read -r os_pkgs npm_pkgs rust_pkgs <<< "$counts"
    if [ "$policy" = "switchyard" ]; then
        if [ "${os_pkgs:-0}" -lt "$MIN_SWITCHYARD_OS_PACKAGES" ]; then
            echo "scan-agent-image.sh: trivy inventoried only ${os_pkgs:-0} OS packages in $image_id (floor $MIN_SWITCHYARD_OS_PACKAGES) — it did not read the proxy OS layer." >&2
            image_failed "inventoried ${os_pkgs:-0} OS packages, below the floor of $MIN_SWITCHYARD_OS_PACKAGES"
            continue
        fi
        if [ "${rust_pkgs:-0}" -lt "$MIN_SWITCHYARD_RUST_PACKAGES" ]; then
            echo "scan-agent-image.sh: trivy inventoried only ${rust_pkgs:-0} Rust packages in $image_id (floor $MIN_SWITCHYARD_RUST_PACKAGES) — it did not read the proxy binary's auditable dependency metadata." >&2
            image_failed "inventoried ${rust_pkgs:-0} Rust packages, below the floor of $MIN_SWITCHYARD_RUST_PACKAGES"
            continue
        fi
        echo "    scanner read $os_pkgs OS and $rust_pkgs Rust packages"
        echo "==> gate: every CRITICAL/HIGH in $tag ($image_id)"
        gate_status=0
        trivy image --skip-version-check --scanners vuln \
            --severity CRITICAL,HIGH --exit-code "$GATE_FINDINGS_EXIT" \
            --format table "$image_id" || gate_status=$?
        record_gate "$gate_status"
    else
        if [ "${os_pkgs:-0}" -lt "$MIN_OS_PACKAGES" ]; then
            echo "scan-agent-image.sh: trivy inventoried only ${os_pkgs:-0} OS packages in $image_id (floor $MIN_OS_PACKAGES) — it did not read the Debian layer. A gate that scans nothing passes everything." >&2
            image_failed "inventoried ${os_pkgs:-0} OS packages, below the floor of $MIN_OS_PACKAGES"
            continue
        fi
        if [ "${npm_pkgs:-0}" -lt "$MIN_NPM_PACKAGES" ]; then
            echo "scan-agent-image.sh: trivy inventoried only ${npm_pkgs:-0} npm packages in $image_id (floor $MIN_NPM_PACKAGES) — it did not analyse the npm tree, which is where every fixable CRITICAL/HIGH in this image has been. A gate blind to an ecosystem passes everything in it." >&2
            image_failed "inventoried ${npm_pkgs:-0} npm packages, below the floor of $MIN_NPM_PACKAGES"
            continue
        fi
        echo "    scanner read $os_pkgs OS and $npm_pkgs npm packages"
        echo "==> gate: CRITICAL/HIGH with a fix available in $tag ($image_id)"
        gate_status=0
        trivy image --skip-version-check --scanners vuln \
            --ignore-unfixed --severity CRITICAL,HIGH \
            --exit-code "$GATE_FINDINGS_EXIT" --format table "$image_id" \
            || gate_status=$?
        record_gate "$gate_status"
    fi

    echo "==> triage input: all CRITICAL/HIGH in $tag (not a gate)"
    trivy image --skip-version-check --scanners vuln \
        --severity CRITICAL,HIGH --exit-code 0 --format table "$image_id"

    if [ "$policy" != "switchyard" ]; then
        agent_images_gated=$((agent_images_gated + 1))
        rm -f "$iidfile"
        iidfile=""
    fi
done

if [ $((agent_images_gated + agent_images_failed)) -ne "$AGENT_IMAGES_DECLARED" ]; then
    echo "scan-agent-image.sh: $agent_images_gated of $AGENT_IMAGES_DECLARED agent images were gated and $agent_images_failed failed before their gate. Refusing to certify a partial run." >&2
    exit 1
fi
# The closing summary: printed only once every image has been visited — gated,
# or recorded as failed before its gate (a build, an inventory, a floor). A run
# that died before it (a triage call, a refused image ID, a timeout) never
# prints it, and kew reads its absence as a scan that stopped before every
# image was gated, whatever an earlier image found (#3195).
if [ "$agent_images_failed" -eq 0 ]; then
    echo "scan-agent-image.sh: gated every image: $agent_images_gated of $AGENT_IMAGES_DECLARED agent images" >&2
else
    echo "scan-agent-image.sh: gated every image: $agent_images_gated of $AGENT_IMAGES_DECLARED agent images, $agent_images_failed failed before their gate" >&2
fi
if [ "$gate_findings" -ne 0 ]; then
    echo "scan-agent-image.sh: the gate found CRITICAL/HIGH findings in $gate_findings image(s):$gate_findings_in" >&2
fi
if [ "$gate_errors" -ne 0 ]; then
    echo "scan-agent-image.sh: the gate could not scan $gate_errors image(s):$gate_errors_in" >&2
fi
if [ "$gate_findings" -ne 0 ] || [ "$gate_errors" -ne 0 ]; then
    exit 1
fi
if [ "$run_switchyard_image" = 0 ]; then
    echo "✓ all $AGENT_IMAGES_DECLARED agent images passed their CRITICAL/HIGH policies; the Switchyard proxy was skipped: $SWITCHYARD_IMAGE_SKIP_REASON"
else
    echo "✓ all $AGENT_IMAGES_DECLARED agent images and the Switchyard proxy passed their CRITICAL/HIGH policies"
fi
