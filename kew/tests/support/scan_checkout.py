"""A synthetic checkout for driving ``scripts/scan-agent-image.sh`` for real.

The gate reads its scanner pin from the tree it runs in and labels each image
with the inputs digest ``kew/src/kew/image_inputs.py`` computes there, so a
copy of the script needs both beside it at the paths kew ships them at. The
paths come from the package itself (:func:`kew.hosts.pin_path`,
:mod:`kew.image_inputs`), so if the script's paths and kew's ever disagree the
tests say so.

The gate identifies trivy by its binary's SHA-256 (#3258), and a stub trivy is
a shell script, never the pinned release binary. :func:`pin_stub_trivy` writes
the stub's digest into the *synthetic* checkout's pin — the one boundary a
test that wants the gate to run past the scanner check stubs. A test about the
binary check itself leaves the shipped digests in place.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

import kew.image_inputs
from kew.hosts import pin_path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_SCRIPT = REPO_ROOT / "scripts" / "scan-agent-image.sh"
TRIVY_PIN = pin_path()
IMAGE_INPUTS_MODULE = Path(kew.image_inputs.__file__).resolve()

_BINARY_DIGEST_LINE = re.compile(
    r"^(TRIVY_BINARY_SHA256_[A-Z0-9_]+)=\S+$", re.MULTILINE
)


def plant_scan_inputs(repo_root: Path) -> Path:
    """Copy the shipped pin and the inputs-digest module into *repo_root*.

    Returns the planted pin file.
    """
    pin = repo_root / TRIVY_PIN.relative_to(REPO_ROOT)
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(TRIVY_PIN.read_text(encoding="utf-8"), encoding="utf-8")
    module = repo_root / IMAGE_INPUTS_MODULE.relative_to(REPO_ROOT)
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(IMAGE_INPUTS_MODULE.read_text(encoding="utf-8"), encoding="utf-8")
    return pin


def synthetic_scan_checkout(repo_root: Path) -> Path:
    """A copy of the gate in *repo_root*, with what it reads beside it."""
    script = repo_root / "scripts" / SCAN_SCRIPT.name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(SCAN_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)
    plant_scan_inputs(repo_root)
    return script


#: The pin key ``scan-agent-image.sh`` reads for each ``uname -s``-``uname -m``.
#: Restated from the script on purpose: the tests that use it are what catch
#: the script's mapping drifting from these platforms.
_PLATFORM_KEYS = {
    "Linux-x86_64": "TRIVY_BINARY_SHA256_LINUX_64BIT",
    "Linux-aarch64": "TRIVY_BINARY_SHA256_LINUX_ARM64",
    "Linux-arm64": "TRIVY_BINARY_SHA256_LINUX_ARM64",
    "Darwin-x86_64": "TRIVY_BINARY_SHA256_MACOS_64BIT",
    "Darwin-arm64": "TRIVY_BINARY_SHA256_MACOS_ARM64",
}


def host_trivy_pin_key() -> str:
    """The ``trivy.pin`` key the gate reads on this host, from ``uname``."""
    uname = os.uname()
    return _PLATFORM_KEYS[f"{uname.sysname}-{uname.machine}"]


def pin_trivy_binaries(repo_root: Path, digest_for: Callable[[str], str]) -> None:
    """Rewrite each ``TRIVY_BINARY_SHA256_*`` line in *repo_root*'s pin to
    ``digest_for(key)``.

    Only a synthetic checkout's copy of the pin is rewritten: the real
    checkout is refused, so a test can never re-pin the shipped scanner. A
    checkout with no pin planted is left without one.
    """
    if repo_root.resolve() == REPO_ROOT.resolve():
        raise ValueError(
            f"refusing to rewrite the scanner pin of the real checkout {REPO_ROOT}"
        )
    pin = repo_root / TRIVY_PIN.relative_to(REPO_ROOT)
    if not pin.exists():
        return
    text = pin.read_text(encoding="utf-8")
    pinned, count = _BINARY_DIGEST_LINE.subn(
        lambda line: f"{line.group(1)}={digest_for(line.group(1))}", text
    )
    assert count == 4, f"{pin} should pin four trivy binaries, found {count}"
    pin.write_text(pinned, encoding="utf-8")


def pin_stub_trivy(repo_root: Path, trivy: Path) -> None:
    """Pin *trivy*'s own SHA-256 as every platform's binary in *repo_root*."""
    digest = hashlib.sha256(trivy.read_bytes()).hexdigest()
    pin_trivy_binaries(repo_root, lambda _key: digest)
