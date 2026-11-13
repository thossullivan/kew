"""What the stub tools read about other processes, on Linux and on macOS.

:mod:`support.fake_systemd` and :mod:`support.fake_docker` answer from real
processes: whether one is running, and which hold a given file open (the
stand-in for a cgroup's membership, :func:`support.fake_systemd.start_in_cgroup`).
Linux answers from ``/proc``. macOS has no ``/proc`` and hands one process no
other's environment, so it answers from ``libproc`` — the calls ``lsof`` makes,
without the second ``lsof`` takes to walk every process — and from ``ps`` for a
zombie, which ``libproc`` will not describe.

The stubs import this module: :func:`stub_prelude` puts it on their path.
"""

from __future__ import annotations

import ctypes
import functools
import os
import subprocess
import sys
from pathlib import Path

_PROC = Path("/proc")

#: ``proc_pidinfo`` flavor: the process's open descriptors, as ``proc_fdinfo``.
_PROC_PIDLISTFDS = 1
#: ``proc_pidfdinfo`` flavor: one descriptor's ``vnode_fdinfo``.
_PROC_PIDFDVNODEINFO = 1
#: ``proc_fdinfo.proc_fdtype`` of a descriptor on a file.
_PROX_FDTYPE_VNODE = 1
#: Where ``vinfo_stat``'s ``vst_dev`` (``uint32_t``) and ``vst_ino``
#: (``uint64_t``) sit in a ``vnode_fdinfo``: after its 24-byte ``proc_fileinfo``.
_VST_DEV = slice(24, 28)
_VST_INO = slice(32, 40)
#: Room for one ``vnode_fdinfo``, which is 176 bytes.
_VNODE_FDINFO_SIZE = 256


def is_linux() -> bool:
    """Whether this host is Linux, where ``/proc`` is always there.

    The condition for skipping a test of a script that only ever runs inside
    kew's Linux sandbox container and reads ``/proc``; the skip itself stays
    in the test, with the reason ``no /proc on this host``. Asked of the
    platform, not of ``/proc``: on a Linux host with ``/proc`` masked the
    test runs, and fails.
    """
    return sys.platform == "linux"


def stub_prelude() -> str:
    """Lines a stub script starts with to import ``support.process_facts``."""
    tests = Path(__file__).resolve().parent.parent
    return f"import sys\nsys.path.append({str(tests)!r})\n"


def alive(pid: int) -> bool:
    """Whether *pid* is running: present and not a zombie awaiting its reaper."""
    if _PROC.is_dir():
        try:
            stat = (_PROC / str(pid) / "stat").read_text()
        except OSError:
            return False
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    probe = subprocess.run(  # noqa: S603 - fixed argv, test support only
        ["/bin/ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode not in (0, 1) or probe.stderr.strip():
        raise OSError(f"ps could not read pid {pid}: {probe.stderr.strip()}")
    state = probe.stdout.strip()
    # Empty: reaped between the signal and the listing.
    return bool(state) and not state.startswith("Z")


def file_identity(path: Path) -> tuple[int, int]:
    """*path*'s device and inode, as :func:`open_files` reports a held file."""
    stat = path.stat()
    return _identity(stat.st_dev, stat.st_ino)


def open_files(pid: int) -> set[tuple[int, int]] | None:
    """The device and inode of every file *pid* holds open.

    ``None`` when its descriptors cannot be read: it is gone, or another
    user's. A zombie holds nothing, so on Linux it is ``None`` too.
    """
    if _PROC.is_dir():
        return _linux_open_files(pid)
    return _darwin_open_files(pid)


def holders(path: Path) -> list[int]:
    """Every running process but this one holding *path* open."""
    wanted = file_identity(path)
    me = os.getpid()
    return [
        pid for pid in _pids() if pid != me and wanted in (open_files(pid) or set())
    ]


def _identity(device: int, inode: int) -> tuple[int, int]:
    # macOS's ``vst_dev`` is unsigned 32-bit; ``st_dev`` may come back signed.
    return device & 0xFFFFFFFF, inode


def _pids() -> list[int]:
    if _PROC.is_dir():
        return [int(entry.name) for entry in _PROC.iterdir() if entry.name.isdigit()]
    libproc = _libproc()
    count = libproc.proc_listallpids(None, 0)
    if count <= 0:
        raise OSError(ctypes.get_errno(), "proc_listallpids")
    # Room for processes started between the two calls.
    buffer = (ctypes.c_int * (count + 256))()
    count = libproc.proc_listallpids(buffer, ctypes.sizeof(buffer))
    if count <= 0:
        raise OSError(ctypes.get_errno(), "proc_listallpids")
    return [pid for pid in buffer[:count] if pid > 0]


def _linux_open_files(pid: int) -> set[tuple[int, int]] | None:
    proc = _PROC / str(pid)
    try:
        if (proc / "stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return None
        descriptors = list((proc / "fd").iterdir())
    except OSError:
        return None
    found: set[tuple[int, int]] = set()
    for descriptor in descriptors:
        try:
            stat = descriptor.stat()
        except OSError:
            continue
        found.add(_identity(stat.st_dev, stat.st_ino))
    return found


def _darwin_open_files(pid: int) -> set[tuple[int, int]] | None:
    libproc = _libproc()
    size = libproc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
    if size <= 0:
        return None
    # ``proc_fdinfo`` is two 32-bit fields: the descriptor, then its type.
    # Room for descriptors opened between the two calls.
    words = (ctypes.c_uint32 * (size // 4 + 128))()
    size = libproc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, words, ctypes.sizeof(words))
    if size <= 0:
        return None
    found: set[tuple[int, int]] = set()
    info = ctypes.create_string_buffer(_VNODE_FDINFO_SIZE)
    for at in range(0, size // 4, 2):
        if words[at + 1] != _PROX_FDTYPE_VNODE:
            continue
        got = libproc.proc_pidfdinfo(
            pid, words[at], _PROC_PIDFDVNODEINFO, info, _VNODE_FDINFO_SIZE
        )
        if got <= 0:
            continue
        raw = info.raw
        found.add(
            _identity(
                int.from_bytes(raw[_VST_DEV], sys.byteorder),
                int.from_bytes(raw[_VST_INO], sys.byteorder),
            )
        )
    return found


@functools.cache
def _libproc() -> ctypes.CDLL:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidfdinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidfdinfo.restype = ctypes.c_int
    return libproc
