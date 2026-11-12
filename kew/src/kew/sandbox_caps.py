"""Lower-only container resource caps, and the rule that keeps them caps (#2360).

The per-container limits in :mod:`kew.sandbox` are HARD ceilings, and the reason
they were never knobs still holds: *a knob is a way to set the cap back to
"none", and the caps exist because "none" is the state containment removes.*

What this module adds is a **lower-only** seam, not a knob. ``[sandbox]
max_memory`` and ``[sandbox] max_cpus`` may move a cap DOWN from its ceiling and
no other way, because the ceilings are sized for a machine, not for a container
VM: on a 16 GiB OrbStack VM only two agents fit at 8 GiB each, so a batch of
review and remediation runs oversubscribes the VM and the host starts to swap.
Fitting the caps to the VM is the operator's job; removing them is nobody's.

Lowering is validated at load and **never silently clamped**. A value above the
ceiling, ``0``, blank, or ``"unlimited"`` is a configuration error: clamping
``16g`` down to ``8g`` would run under a cap the operator never asked for and
hide the mistake that produced it, and accepting ``"unlimited"`` as "the
ceiling" would answer the containment question by accident.

The ``tmpfs`` sizes derive from the memory cap (:func:`derived_tmpfs_size`)
rather than standing beside it: tmpfs pages are host RAM charged to the
container's memory cgroup, so a tmpfs larger than the memory cap is a route to
filling HOST memory that ``--memory`` does not close.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from kew.runtime_names import OPENSHELL_RUNTIME

#: The memory ceiling. ``max_memory`` may lower it; nothing raises it.
HARD_MAX_MEMORY: Final = "8g"
#: The CPU ceiling, before the per-launch clamp to the daemon's CPU count.
HARD_MAX_CPUS: Final = 4
#: The floor a lowered memory cap may not go under. A container below this
#: cannot run the toolchains kew's own agents run (uv, npm, a test suite), so a
#: smaller value is a cap that refuses every launch rather than sizing one.
MIN_MAX_MEMORY: Final = "1g"

# Docker's own size grammar: an integer with an optional b/k/m/g unit, bytes
# when the unit is omitted. Anything else is rejected rather than guessed —
# "8gb", "unlimited" and "" are all values Docker would refuse at launch, and
# a refusal at launch is a refusal with no container.
_SIZE_PATTERN = re.compile(r"\A(?P<value>[0-9]+)(?P<unit>[bkmgBKMG]?)\Z")
_SIZE_UNITS: Final[dict[str, int]] = {
    "b": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
}

_MEMORY_RULE: Final = (
    "[sandbox] max_memory is lower-only: a Docker size from "
    f"{MIN_MAX_MEMORY} up to the hard {HARD_MAX_MEMORY} cap (for example "
    '"3g" or "1536m"). There is no value that raises or removes the cap'
)
_CPU_RULE: Final = (
    "[sandbox] max_cpus is lower-only: a whole number of CPUs from 1 up to "
    f"the hard cap of {HARD_MAX_CPUS}. There is no value that raises or "
    "removes the cap"
)


def parse_memory_size(value: str) -> int:
    """Bytes named by a Docker size string (``"3g"``, ``"1536m"``, ``"512"``)."""
    match = _SIZE_PATTERN.match(value.strip())
    if match is None:
        raise ValueError(f"{value!r} is not a Docker size")
    size = int(match["value"]) * _SIZE_UNITS[(match["unit"] or "b").lower()]
    if size <= 0:
        raise ValueError(f"{value!r} is not a positive size")
    return size


def format_memory_size(size: int) -> str:
    """The shortest exact size string for *size* bytes, in BOTH size grammars.

    Deliberately no ``b`` suffix on the fallback. These strings are spent on a
    ``--tmpfs ...:size=`` mount option as well as on ``--memory``, and the
    kernel's tmpfs parser is the stricter of the two: it takes a bare byte
    count or a ``k``/``m``/``g`` suffix and refuses ``size=1000000000b``
    outright. A bare byte count means bytes to both, so it is the one spelling
    of a non-round size that cannot turn a load-accepted ``max_memory`` (a
    bare byte count is one) into a cap that refuses every launch.
    """
    for unit in ("g", "m", "k"):
        scale = _SIZE_UNITS[unit]
        if size % scale == 0:
            return f"{size // scale}{unit}"
    return str(size)


def memory_size_label(value: str) -> str:
    """*value* (a Docker size) as a person reads it in a brief: ``"8 GB"``.

    Binary units, as Docker reads the size: ``"1536m"`` is ``"1536 MB"``, never
    a rounded ``"1.5 GB"`` the cgroup does not enforce.
    """
    size = parse_memory_size(value)
    for unit, label in (("g", "GB"), ("m", "MB"), ("k", "KB")):
        scale = _SIZE_UNITS[unit]
        if size % scale == 0:
            return f"{size // scale} {label}"
    return f"{size} bytes"


def enforced_memory_cap(max_memory: str | None, *, runtime: str) -> str | None:
    """The per-container memory cap kew enforces on *runtime*, as a brief says it.

    ``None`` on OpenShell: its create carries no memory flag, so kew enforces no
    cap there whatever ``[sandbox] max_memory`` says (see
    ``kew.sandbox._attested_caps``). A brief that named one would tell the
    agent about a limit that does not exist (#3201).
    """
    if runtime == OPENSHELL_RUNTIME:
        return None
    return memory_size_label(max_memory or HARD_MAX_MEMORY)


def derived_tmpfs_size(memory: str, ceiling: str, divisor: int) -> str:
    """A tmpfs size that stays under the *memory* cap, and under *ceiling*.

    ``min(ceiling, memory / divisor)``: a lowered memory cap pulls the mounts
    down with it, so filling one is still an OOM kill of the container rather
    than of the host, while the default cap keeps today's pinned sizes exactly.
    """
    return format_memory_size(
        min(parse_memory_size(ceiling), parse_memory_size(memory) // divisor)
    )


def validated_max_memory(value: object) -> str:
    """Return the operator's ``max_memory``, or raise the lower-only rule.

    Raised as ``ValueError`` from a pydantic validator, so an out-of-range
    value is a ``ConfigError`` at load — the operator learns the rule once,
    before any container is launched under a cap they did not choose.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(_MEMORY_RULE)
    text = str(value).strip()
    try:
        size = parse_memory_size(text)
    except ValueError as exc:
        raise ValueError(f"{_MEMORY_RULE} ({exc})") from exc
    if (
        not parse_memory_size(MIN_MAX_MEMORY)
        <= size
        <= parse_memory_size(HARD_MAX_MEMORY)
    ):
        raise ValueError(f"{_MEMORY_RULE}; got {text!r}")
    return text


def validated_max_cpus(value: object) -> int:
    """Return the operator's ``max_cpus``, or raise the lower-only rule."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(_CPU_RULE)
    if not 1 <= value <= HARD_MAX_CPUS:
        raise ValueError(f"{_CPU_RULE}; got {value}")
    return value


@dataclass(frozen=True)
class ResourceCaps:
    """The effective per-container caps one launch runs under.

    One value object rather than four parameters: the memory cap and the two
    tmpfs sizes are a single consistent set (the mounts are sized FROM the
    memory cap), and a caller that could pass three of the four could pass a
    tmpfs larger than the memory cap it belongs to.
    """

    memory: str
    cpus: int
    home_tmpfs: str
    tmp_tmpfs: str

    @property
    def memory_swap(self) -> str:
        """Equal to :attr:`memory` — how Docker spells "no swap".

        Structural rather than a second constant: with a swap allowance a
        memory-bomb agent survives the cgroup limit and pushes the HOST into
        swap-thrash, so the cap would move the failure instead of removing it.
        Deriving it here is what makes that true at every value of *memory*.
        """
        return self.memory
