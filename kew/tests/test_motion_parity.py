"""The backend and the browser agree on what is MOVING (#2305, AC1).

``kew/src/kew/motion.py`` decides whether a status word is ``executing``,
``waiting_kew``, ``waiting_human`` or ``settled``, and serves it on the run,
loop and node DTOs. ``frontend/src/lib/factory/motion.ts`` holds the same table
for the surfaces that have a status word and no DTO — an edge's target, a
fixture, a replayed tick from a server older than the field.

Two tables, one vocabulary: if they drift, the console animates something the
server does not think is running, which is the exact failure #2305 is about
(``metro.ts`` drew a cyan edge into ``in_review``, a published pull request
waiting for a person, while missing ``adversarial_review`` and ``remediating``).
A drift in the other direction is just as bad and quieter: a status whose halo
the server grants and the browser withholds.

The frontend cannot import a Python module, so this test reads the TypeScript —
the same anchoring ``test_factory_status_parity.py`` uses for the mark table.
It checks five things:

1. the two ``MOTION_OF`` tables are the same mapping, word for word;
2. every member of :class:`DriverStatus`, ``RunStatus`` and ``LoopStatus`` is in
   it (a word neither side mapped would fall back silently, and the fallback is
   a claim about a status kew has NOT shipped);
3. the two ``RUN_MOTION_OF`` tables — the run vocabulary's reading of a word the
   shared table maps for a node — are the same mapping too, and refine only
   words that table has;
4. both sides fall back to the same word for one they have not heard of;
5. both sides admit the same four motions.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from kew.graph.driver.overlay import DriverStatus
from kew.motion import FALLBACK_MOTION, MOTION_OF, MOTIONS, RUN_MOTION_OF
from kew.run_store import LoopStatus, RunStatus

_MOTION_TS = "frontend/src/lib/factory/motion.ts"

# A `key: 'motion',` entry in MOTION_OF. Unquoted keys only — every entry in the
# table is a bare identifier, so a quoted key is a style break worth failing on
# rather than a parse gap to tolerate.
_ENTRY_RE = re.compile(r"^\s{2}([a-z][a-z0-9_]*):\s*'([a-z_]+)',?\s*$", re.MULTILINE)
# The `Motion` union: `'executing' | 'waiting_kew' | ...`.
_UNION_RE = re.compile(r"export type Motion =([^;]+);")
_WORD_RE = re.compile(r"'([a-z_]+)'")
_FALLBACK_RE = re.compile(r"export const FALLBACK_MOTION: Motion = '([a-z_]+)'")


def _motion_ts() -> str:
    """The source of motion.ts, located by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _MOTION_TS
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    pytest.skip(f"{_MOTION_TS} not present in this checkout")


def _frontend_table(name: str = "MOTION_OF") -> dict[str, str]:
    """One `status: 'motion'` table in motion.ts, parsed from its source."""
    _, _, after = _motion_ts().partition(
        f"export const {name}: Record<string, Motion> = {{"
    )
    body, sep, _ = after.partition("\n};")
    assert sep, f"{name} table not found in {_MOTION_TS}"
    table = dict(_ENTRY_RE.findall(body))
    assert table, f"parsed no entries out of {name} in {_MOTION_TS}"
    return table


def _shipped_statuses() -> dict[str, set[str]]:
    """Every status word the three vocabularies can serve, by vocabulary."""
    return {
        "DriverStatus": {s.value for s in DriverStatus},
        "RunStatus": set(typing.get_args(RunStatus)),
        "LoopStatus": set(typing.get_args(LoopStatus)),
    }


def test_motion_tables_are_the_same_mapping() -> None:
    """One vocabulary, two copies, no drift — in either direction."""
    frontend = _frontend_table()
    backend = dict(MOTION_OF)
    shared = set(backend) & set(frontend)
    disagree = sorted(word for word in shared if backend[word] != frontend[word])
    assert frontend == backend, (
        f"{_MOTION_TS} and kew/motion.py disagree about motion. "
        f"Only the backend maps {sorted(set(backend) - set(frontend))}; "
        f"only the frontend maps {sorted(set(frontend) - set(backend))}; "
        f"they map differently: {disagree}"
    )


def test_both_tables_cover_every_shipped_status() -> None:
    """A status kew ships must never take the unknown-word fallback.

    The fallback exists for a state a LATER kew adds. Used for one that is
    already served it is a lie with no symptom: the item simply never moves (or
    never stops), and nothing but the word says why.
    """
    frontend = _frontend_table()
    for name, words in _shipped_statuses().items():
        missing_backend = sorted(w for w in words if w not in MOTION_OF)
        missing_frontend = sorted(w for w in words if w not in frontend)
        assert not missing_backend, (
            f"kew/motion.py has no motion for {name} {missing_backend} — add each "
            "to MOTION_OF with the motion it deserves, and to the same table in "
            f"{_MOTION_TS}."
        )
        assert not missing_frontend, (
            f"{_MOTION_TS} has no motion for {name} {missing_frontend} — add each "
            "to its MOTION_OF table."
        )


def test_the_run_refinement_tables_are_the_same_mapping() -> None:
    """The one word the two vocabularies spell alike, read alike on both sides.

    A queued NODE waits on a person to arm it; a queued RUN is the console's own
    display word for an untouched `pending` row the dispatcher starts by itself.
    One side splitting that and the other not is the same drift as the main
    table's, on the word most likely to be read by the wrong reader.
    """
    frontend = _frontend_table("RUN_MOTION_OF")
    assert frontend == dict(RUN_MOTION_OF), (
        f"{_MOTION_TS} and kew/motion.py disagree about the run vocabulary's "
        f"readings: frontend {frontend}, backend {dict(RUN_MOTION_OF)}"
    )


def test_every_run_refinement_refines_a_word_the_shared_table_maps() -> None:
    """Refinements only — a run-only word belongs in `MOTION_OF` itself.

    `RUN_MOTION_OF` exists for a COLLISION: a word both vocabularies spell and
    read differently. A key the shared table has never heard of is not a
    collision, and putting it here would hide it from every epic surface.
    """
    frontend = _frontend_table()
    for word in RUN_MOTION_OF:
        assert word in MOTION_OF, (
            f"kew/motion.py refines {word!r} for the run vocabulary but "
            "MOTION_OF has no reading of it to refine"
        )
        assert word in frontend, f"{_MOTION_TS}'s MOTION_OF has no {word!r}"
        assert RUN_MOTION_OF[word] != MOTION_OF[word], (
            f"{word!r} reads the same in both vocabularies — drop it from "
            "RUN_MOTION_OF rather than keeping a refinement that refines nothing"
        )


def test_every_mapped_motion_is_one_of_the_four_words() -> None:
    """Neither table may invent a fifth motion: nothing would animate it."""
    frontend = {**_frontend_table(), **_frontend_table("RUN_MOTION_OF")}
    backend = {**MOTION_OF, **RUN_MOTION_OF}
    assert {m for m in backend.values() if m not in MOTIONS} == set()
    unknown = sorted({m for m in frontend.values() if m not in MOTIONS})
    assert not unknown, f"{_MOTION_TS} maps statuses to unknown motions {unknown}"


def test_both_sides_admit_the_same_four_motions() -> None:
    """The `Motion` union and the `Motion` Literal are one type."""
    union = _UNION_RE.search(_motion_ts())
    assert union, f"the Motion union was not found in {_MOTION_TS}"
    assert set(_WORD_RE.findall(union.group(1))) == set(MOTIONS)


def test_both_sides_fall_back_to_the_same_word() -> None:
    """An unknown status degrades identically on both sides.

    A server and a browser that disagree about the fallback disagree about
    every status one of them has not shipped yet — which is precisely when
    nobody is looking.
    """
    fallback = _FALLBACK_RE.search(_motion_ts())
    assert fallback, f"FALLBACK_MOTION was not found in {_MOTION_TS}"
    assert fallback.group(1) == FALLBACK_MOTION


def test_the_fallback_neither_moves_nor_claims_to_be_over() -> None:
    """The one invariant the fallback's VALUE has to keep.

    It is read for a status this kew has never heard of, so it must not earn a
    halo (`executing`) and must not report `alive=False` about work still in
    flight (`settled`).
    """
    assert FALLBACK_MOTION not in {"executing", "settled"}
