"""The digest of the agent image build inputs, and the label that carries it.

Standard library only, so ``scripts/scan-agent-image.sh`` can run this file
with the host's ``python3 -I`` and label the images it builds with the same
value ``kew sandbox build`` does — one implementation, not a second copy in
shell that could drift from it. Run as a script, it prints the ``--label``
value for the checkout named on its command line::

    python3 -I -B kew/src/kew/image_inputs.py <repo-root>
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import unicodedata
from pathlib import Path

#: The image label every agent image kew or the scan gate builds carries: the
#: full :func:`agent_image_inputs_digest` of the inputs it was built from. A
#: ``docker-<12 hex>`` pin tag only names inputs; the label is what the image
#: says it was built from, and a launch refuses a pin tag whose label disagrees.
AGENT_IMAGE_INPUTS_LABEL = "dev.kew.agent-image-inputs"


def agent_image_inputs_digest(repo_root: Path) -> str:
    """Hash regular files under ``docker`` and refuse other entry types.

    The encoding includes entry kind, path length and bytes, and the executable
    bit (never the full mode, which would fold the checkout's umask into the pin).
    Files include content length and bytes. Directories are ignored.

    Pins use ``docker-<first 12 hex of the digest>``. This identity survives squash
    merges, covers uncommitted edits, and does not need git history.
    """
    docker_root = repo_root / "docker"
    entries: list[tuple[str, Path, bytes]] = []
    for path in docker_root.rglob("*"):
        # NFC-normalise the name: git stores one pathname, but macOS may hand
        # back a decomposed spelling of the same characters, and the pin must
        # not depend on which filesystem produced the checkout.
        relative = unicodedata.normalize("NFC", path.relative_to(repo_root).as_posix())
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"symbolic link under docker: {relative}")
        if stat.S_ISREG(mode):
            entries.append((relative, path, b"F"))
        elif not stat.S_ISDIR(mode):
            raise ValueError(f"unsupported entry type under docker: {relative}")

    digest = hashlib.sha256()
    for relative, path, kind in sorted(entries):
        path_bytes = os.fsencode(relative)
        payload = path.read_bytes()

        digest.update(kind)
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        # Only the executable bit: that is all git records and all a Dockerfile
        # COPY preserves, and it is the same on every checkout. The full mode
        # would fold the operator's umask into the pin, so a contributor with
        # umask 077 would compute a different digest from CI for identical
        # inputs and the ratchet would go red on their machine alone.
        executable = bool(path.lstat().st_mode & 0o111)
        digest.update(b"X" if executable else b"-")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def inputs_label(repo_root: Path) -> str:
    """The ``--label`` value an image built from *repo_root*'s inputs carries."""
    return f"{AGENT_IMAGE_INPUTS_LABEL}={agent_image_inputs_digest(repo_root)}"


def main(argv: list[str]) -> int:
    """Print the inputs label for the checkout in ``argv[1]``."""
    if len(argv) != 2:
        print("usage: image_inputs.py <repo-root>", file=sys.stderr)
        return 2
    try:
        print(inputs_label(Path(argv[1])))
    except (OSError, ValueError) as exc:
        print(f"image_inputs.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
