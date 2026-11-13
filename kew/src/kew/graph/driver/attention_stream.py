"""What one `kew epic watch` process has already said — and what every process
before it said (#2202).

`--follow` used to keep that memory only in RAM, so each restart re-emitted
every still-open state and the operator wrapper had to keep a state file of its
own. The memory now lives under the epic's state directory, beside its driver
lock: one record per item, holding the state that item was last EMITTED in.

What makes a state distinct is `AttentionEvent.state_key` — kind, item, reason
and identity — so that is what a record holds. Keying on the identity alone
would drop a real event: a `sandbox_containment` refusal and the
`review_launch_failed` hold that replaced it both carry an empty identity, and
only their reason tells them apart.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kew.graph.driver.attention import AttentionEvent
from kew.progress import _warn

#: Bumped when the record shape changes. A file written by another version is
#: read as NO memory (every open state replays, out loud) rather than guessed at.
MEMORY_VERSION = 1

#: kind, reason, identity — the state one item was last emitted in.
ItemState = tuple[str, str, str]

_FIELDS = ("kind", "reason", "identity")


def _state_of(event: AttentionEvent) -> ItemState:
    """The distinctness of this event, minus the item it is keyed by."""
    return (event.kind.value, event.reason, event.identity)


def _state_from(raw: Any) -> ItemState | None:
    """One record read back, or None for anything not shaped like one."""
    if not isinstance(raw, dict):
        return None
    values = [raw.get(field_name) for field_name in _FIELDS]
    if not all(isinstance(value, str) for value in values):
        return None
    kind, reason, identity = values
    return (str(kind), str(reason), str(identity))


#: There is no document to judge: the file is absent, or unreadable and
#: already named on stderr. A file that PARSES — to `null`, to a list, to
#: anything — is a document and is rejected out loud like any other shape that
#: is not a memory, because a present file the watch ignores in silence reads
#: as a watch that lost its memory for no reason.
_NOTHING = object()


def _read_document(path: Path, *, epic: str) -> Any:
    """The memory file parsed, or `_NOTHING` when there is no document to judge.

    Unreadable is not silent: a corrupt file read as "everything already said"
    would swallow the events the watch exists to show.
    """
    if not path.exists():
        # The first watch of an epic has no memory: that is the normal start,
        # not a failure, and every open state is rightly emitted.
        return _NOTHING
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(f"epic {epic}: unreadable watch memory at {path} ({exc}) — replaying")
        return _NOTHING


def _rejection(document: dict[Any, Any], *, epic: str) -> str | None:
    """Why this document is not a memory this watch may read, or None.

    The epic is stamped in the file because two epic names can share one
    filename slug; another epic's memory must replay, never silence.
    """
    version = document.get("version")
    if version != MEMORY_VERSION:
        return f"it was written by memory version {version!r}, not {MEMORY_VERSION}"
    written_for = document.get("epic")
    if written_for != epic:
        return f"it belongs to epic {written_for!r}"
    if not isinstance(document.get("items"), dict):
        return "its records are missing or not a table"
    return None


def _memory_items(document: Any, *, epic: str) -> dict[Any, Any]:
    """The records of a memory written for THIS epic by THIS version.

    A REJECTED memory is as loud as an unreadable one. Every path here replays
    every open state, and a watch that replays without saying why reads as one
    that lost its memory for no reason — which is what sent an operator looking
    at the wrapper's state file instead of at this one.
    """
    if document is _NOTHING:
        # Absent (the normal first watch) or already said out loud by
        # _read_document — either way there is nothing to add.
        return {}
    rejection = (
        _rejection(document, epic=epic)
        if isinstance(document, dict)
        else "it is not a memory document"
    )
    if rejection is not None:
        _warn(f"epic {epic}: ignoring the watch memory — {rejection}, replaying")
        return {}
    items: dict[Any, Any] = document["items"]
    return items


def load_memory(path: Path, *, epic: str) -> dict[str, ItemState]:
    """What the last watch emitted per item, or `{}` when there is no usable
    memory — which replays every open state rather than dropping it.

    A record that does not read back costs only its own item's silence, and says
    so too: dropped quietly, that item's open state would repeat on every
    restart with nothing to explain it."""
    items = _memory_items(_read_document(path, epic=epic), epic=epic)
    memory: dict[str, ItemState] = {}
    dropped = 0
    for item, raw in items.items():
        state = _state_from(raw) if isinstance(item, str) else None
        if state is None:
            dropped += 1
            continue
        memory[item] = state
    if dropped:
        plural = "" if dropped == 1 else "s"
        _warn(
            f"epic {epic}: {dropped} unreadable watch memory record{plural} at "
            f"{path} — replaying those items"
        )
    return memory


def save_memory(path: Path, *, epic: str, items: dict[str, ItemState]) -> None:
    """Replace the memory with this poll's states, raising `OSError` when it
    cannot be written — the caller says so out loud. One `os.replace` per poll,
    so a concurrent watch on the same epic reads a whole file; the later writer
    simply wins."""
    document = {
        "version": MEMORY_VERSION,
        "epic": epic,
        "items": {
            item: dict(zip(_FIELDS, state, strict=True))
            for item, state in items.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, sort_keys=True))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass
class AttentionStream:
    """One watch's memory of what it has already said, persisted or not.

    `--follow` used to exit on its first event, so an operator ran it in a
    restart loop and every restart re-emitted every still-open state — the
    #2174 `publication_held` event arrived four times in 30 minutes. Following
    properly means each OPEN state is emitted once, keyed on what makes a state
    distinct (#2183), and remembered across restarts (#2202).
    """

    epic: str = ""
    #: Where the memory persists; None for a snapshot watch, which keeps none.
    path: Path | None = None
    seen: dict[str, ItemState] = field(default_factory=dict)
    emitted: bool = False
    #: Open states this process suppressed on its first poll as already said.
    resumed: int = 0
    polls: int = 0
    warned: bool = False

    @classmethod
    def following(cls, epic: str, path: Path, *, replay: bool) -> AttentionStream:
        """A `--follow` stream that resumes the persisted memory — unless
        `--replay` was asked for, which starts from nothing said."""
        seen = {} if replay else load_memory(path, epic=epic)
        return cls(epic=epic, path=path, seen=seen)

    def fresh(self, events: tuple[AttentionEvent, ...]) -> tuple[AttentionEvent, ...]:
        """The events not already emitted for their item, remembering them.

        The memory spans only states still in the stream: a state that CLEARS
        and later RECURS is a second, real thing for a person, and many kinds
        carry no identity to tell the two apart (a `sandbox_containment` refusal
        after an operator `unhold` is byte-identical to the first one). So this
        poll's states REPLACE the last poll's rather than accumulating — an
        accumulating memory silenced every such recurrence for the life of the
        watch.

        Nothing is WRITTEN here — `commit` does that, once the caller has
        emitted. A memory written first records as "already said" an event that
        a failed emit never printed, and the restarted watch is silent about it.
        """
        current = {event.item: _state_of(event) for event in events}
        new = tuple(
            event
            for event in events
            if current[event.item] != self.seen.get(event.item)
        )
        self.polls += 1
        self.seen = current
        self.emitted = self.emitted or bool(new)
        if self.polls == 1:
            self.resumed = len(events) - len(new)
        return new

    def commit(self) -> None:
        """Persist this poll's states, AFTER the caller has emitted them.

        An emit that raises — a `BrokenPipeError` from a dead `--json`
        consumer, a full disk under a redirect — therefore leaves the memory at
        the last poll that made it out, and the restarted watch replays the
        event instead of swallowing it. Committing late costs at most a repeat;
        committing early costs the event itself.

        Says so ONCE if it cannot be written: a watch that silently keeps no
        memory would repeat itself on every restart.
        """
        if self.path is None:
            return
        try:
            save_memory(self.path, epic=self.epic, items=self.seen)
        except OSError as exc:
            if not self.warned:
                self.warned = True
                _warn(
                    f"epic {self.epic}: could not write the watch memory at "
                    f"{self.path} ({exc}) — a restart will repeat these events"
                )
