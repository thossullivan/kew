"""A fake ``launchctl`` on PATH for the `kew drive install` tests (#2810).

No test touches the real launchd. The shim keeps launchd's facts a test
needs — which labels are loaded, and the definition each was loaded from — as
files in its own directory: ``bootstrap`` loads a copy of the plist under its
label (``loaded.<label>``), ``bootout`` unloads it, and ``print`` exits 0 only
for a loaded label (113, as launchd does, otherwise), printing the loaded
definition as launchd does: ``ProgramArguments`` in an ``arguments = { ... }``
block, ``EnvironmentVariables`` (beside launchd's own ``XPC_SERVICE_NAME``) in
an ``environment`` block, the working directory and log paths, the launch
policy among the job's ``properties``, and a ``KeepAlive`` dictionary's
conditions as ``NAME => 0|1`` in a ``semaphores`` block.
``print-disabled`` prints the ``disabled`` file. Every argv it is handed is
appended to ``calls``.

Like the gcloud shim it needs an executable bit that works; on a ``noexec``
pytest temporary filesystem it goes under ``HOME`` instead and is removed on
teardown.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from kew import subprocess_utils
from support.fake_gcloud import launchable

FAKE_LAUNCHCTL = r"""#!/bin/sh
here="$(dirname "$0")"
echo "$*" >> "$here/calls"
case "$1" in
  print)
    loaded="$here/loaded.${2##*/}"
    if [ ! -f "$loaded" ]; then echo "Could not find service" >&2; exit 113; fi
    exec "@PYTHON@" -I "$here/print-job.py" "$2" "$loaded" ;;
  print-disabled)
    cat "$here/disabled" 2>/dev/null
    exit 0 ;;
  bootstrap)
    label="$(basename "$3" .plist)"
    cp "$3" "$here/loaded.$label"
    exit 0 ;;
  bootout)
    rm -f "$here/loaded.${2##*/}"
    exit 0 ;;
esac
exit 0
"""

# ``launchctl print`` for one loaded definition, laid out as launchd lays it out.
PRINT_JOB = r"""import plistlib, sys
target, loaded = sys.argv[1:]
with open(loaded, "rb") as f:
    job = plistlib.load(f)
print(target + " = {")
print("\targuments = {")
for argument in job.get("ProgramArguments", []):
    print("\t\t" + argument)
print("\t}")
print("\tdefault environment = {")
print("\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin")
print("\t}")
print("\tenvironment = {")
print("\t\tXPC_SERVICE_NAME => " + job.get("Label", ""))
for name, value in job.get("EnvironmentVariables", {}).items():
    print("\t\t" + name + " => " + value)
print("\t}")
for key, name in (
    ("WorkingDirectory", "working directory"),
    ("StandardOutPath", "stdout path"),
    ("StandardErrorPath", "stderr path"),
):
    if key in job:
        print("\t" + name + " = " + job[key])
policy = [
    p for k, p in (("KeepAlive", "keepalive"), ("RunAtLoad", "runatload")) if job.get(k)
]
print("\tproperties = " + " | ".join([*policy, "inferred program"]))
conditions = {"SuccessfulExit": "successful exit"}
if isinstance(job.get("KeepAlive"), dict):
    print("\tsemaphores = {")
    for key, value in job["KeepAlive"].items():
        print("\t\t" + conditions.get(key, key) + " => " + str(int(value)))
    print("\t}")
print("}")
"""


@contextmanager
def fake_launchctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Install the shim as this process's ``launchctl``; yield its directory."""
    fallback: str | None = None
    if launchable(tmp_path):
        bin_dir = tmp_path / "fake-launchctl"
        bin_dir.mkdir()
    else:
        fallback = tempfile.mkdtemp(prefix="kew-fake-launchctl-", dir=Path.home())
        bin_dir = Path(fallback)
    shim = bin_dir / "launchctl"
    shim.write_text(
        FAKE_LAUNCHCTL.replace("@PYTHON@", sys.executable), encoding="utf-8"
    )
    (bin_dir / "print-job.py").write_text(PRINT_JOB, encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "launchctl", str(shim))
    try:
        yield bin_dir
    finally:
        if fallback is not None:
            shutil.rmtree(fallback, ignore_errors=True)


def calls(bin_dir: Path) -> list[str]:
    log = bin_dir / "calls"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
