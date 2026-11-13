"""The factory UI knows every status the backend can serve (FUI-04, #2068).

``frontend/src/lib/factory/status.ts`` maps a status word to a mark — a shape
and a colour — and deliberately falls back to the muted todo ring for a word it
does not recognise, so a server that grows a state renders honestly instead of
blanking a row. That fallback is right for a state kew has not shipped; it is a
bug for one it has, because the mark then says "nothing has started" about a
live adversarial-review round or a run stopped at its budget cap, and nothing
but the word tells the two apart.

The frontend cannot import a Python enum, and its own test was anchored to
``STATUS_COLOR`` in ``epic/render.ts`` — a frontend constant covering eight of
the fourteen ``DriverStatus`` members, so it passed while five statuses painted
as todo. This module is the missing anchor: it reads the TypeScript and fails if
:class:`DriverStatus`, ``RunStatus`` or ``LoopStatus`` holds a word the mark
table, or the frontend test's transcription of that vocabulary, has never heard
of. The ``publication`` field of ``WorkArtifactDTO`` is anchored the same way —
it is a bare ``str`` with no Literal to import, so the check reads the source of
the builder that fills it.

The same anchor is extended to one derived reading: ``LOOP_GATE`` in
``frontend/src/lib/factory/loops.ts``, which turns a ``LoopStatus`` into the
loops page's verification line. It has the same forward-compatible fallback
("gate pending") and therefore the same failure mode — a shipped terminal status
nobody mapped reads as still-in-progress forever, and the page has already
stopped polling by then.

``LOOP_TERMINAL`` in the same file is anchored to ``_RESUMABLE`` in
``kew/loops/delegate.py`` for the same reason. That set is what arms and disarms
the loops page's 4s poll, and it excluded ``needs_human`` and ``paused`` on the
stated rule that a resumable loop must keep polling — while listing
``budget_limited``, which ``kew loop resume --budget`` restarts, so the page
disarmed itself for a loop that went back to ``pursuing`` behind its back.

The committed fixture set is anchored the same way at the end of this module,
for the readings of it the host DERIVES rather than chooses: the wording
``_rule_in_flight`` builds out of an execution dimension's own ``run_status``;
``PENDING_RUN_MAX_AGE_S``, the bound ``fixtures.test.ts`` holds every in-flight
fixture row inside; and the two readings taken straight off the committed run
ledger — a work view's execution dimension (``_execution_dto`` of the newest
row on that item at the instant the view was composed) and every committed
reading of ``fleet_status(store).pending`` (``len(store.list_pending())``: the
root of ``/api/fleet/status``, a dashboard's ``glance.fleet`` and the health
report's ``fleet``). All of them are #2377
— the fixtures carried a status the enum has never held, and correcting the
status left the derived sentence, the pending count and the compose ORDER
standing beside it.

Nothing here asserts WHICH mark a status gets — that is a design decision, and
``status.test.ts`` pins it. This test only asserts that the decision was made,
plus the invariants a design decision cannot break: no terminal loop status may
read as pending, and no resumable one may be terminal at all.
"""

from __future__ import annotations

import ast
import json
import re
import typing
from datetime import datetime
from pathlib import Path

import pytest
from support.source_ast import calls, definition, dotted

from kew.graph.driver.overlay import DriverStatus
from kew.loops.delegate import _RESUMABLE
from kew.run_store import LoopStatus, RunRecord, RunStatus
from kew.service import work as work_service
from kew.service.work import PENDING_RUN_MAX_AGE_S

_STATUS_TS = "frontend/src/lib/factory/status.ts"
_STATUS_TEST_TS = "frontend/src/lib/factory/status.test.ts"
_LOOPS_TS = "frontend/src/lib/factory/loops.ts"
_LOOPS_TEST_TS = "frontend/src/lib/factory/loops.test.ts"
_RUN_STATUS_TS = "frontend/src/lib/run-status.ts"

# A `key: 'meaning',` entry in MEANING_OF. Unquoted keys only — every entry in
# the table is a bare identifier, so a quoted key would be a style break worth
# failing on rather than a parse gap to tolerate.
_ENTRY_RE = re.compile(r"^\s{2}([a-z][a-z0-9_]*):\s*'([a-z_]+)',?\s*$", re.MULTILINE)
# A `'status',` element of one of the test's vocabulary arrays.
_ARRAY_ITEM_RE = re.compile(r"'([a-z][a-z0-9_]*)'")


def _frontend_file(relative: str) -> Path:
    """Locate a frontend source file by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} not present in this checkout")


def _meaning_of() -> dict[str, str]:
    """The `MEANING_OF` table in status.ts, parsed from its source."""
    text = _frontend_file(_STATUS_TS).read_text(encoding="utf-8")
    _, _, after = text.partition("const MEANING_OF: Record<string, StatusMeaning> = {")
    body, sep, _ = after.partition("\n};")
    assert sep, f"MEANING_OF table not found in {_STATUS_TS}"
    table = {key: meaning for key, meaning in _ENTRY_RE.findall(body)}
    assert table, f"parsed no entries out of MEANING_OF in {_STATUS_TS}"
    return table


def _run_meaning_of() -> dict[str, str]:
    """The `RUN_MEANING_OF` override table in status.ts, parsed the same way."""
    text = _frontend_file(_STATUS_TS).read_text(encoding="utf-8")
    _, _, after = text.partition(
        "const RUN_MEANING_OF: Record<string, StatusMeaning> = {"
    )
    body, sep, _ = after.partition("\n};")
    assert sep, f"RUN_MEANING_OF table not found in {_STATUS_TS}"
    table = {key: meaning for key, meaning in _ENTRY_RE.findall(body)}
    assert table, f"parsed no entries out of RUN_MEANING_OF in {_STATUS_TS}"
    return table


# A `key: { kind: 'x', word: '...' },` entry in the LOOP_GATE table in loops.ts.
_GATE_ENTRY_RE = re.compile(
    r"^\s{2}([a-z][a-z0-9_]*):\s*\{\s*kind:\s*'([a-z_]+)'", re.MULTILINE
)


def _loop_gate() -> dict[str, str]:
    """The `LOOP_GATE` table in loops.ts: loop status -> evidence meaning."""
    text = _frontend_file(_LOOPS_TS).read_text(encoding="utf-8")
    _, _, after = text.partition("const LOOP_GATE: Record<string, LoopGate> = {")
    body, sep, _ = after.partition("\n};")
    assert sep, f"LOOP_GATE table not found in {_LOOPS_TS}"
    table = {key: kind for key, kind in _GATE_ENTRY_RE.findall(body)}
    assert table, f"parsed no entries out of LOOP_GATE in {_LOOPS_TS}"
    return table


def _loop_terminal() -> set[str]:
    """The `LOOP_TERMINAL` set in loops.ts, the states that stop the poll."""
    text = _frontend_file(_LOOPS_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition(
        "export const LOOP_TERMINAL: ReadonlySet<string> = new Set(["
    )
    assert sep, f"LOOP_TERMINAL not found in {_LOOPS_TS}"
    body, sep, _ = after.partition("]")
    assert sep, f"LOOP_TERMINAL not terminated in {_LOOPS_TS}"
    words = set(_ARRAY_ITEM_RE.findall(body))
    assert words, f"parsed no statuses out of LOOP_TERMINAL in {_LOOPS_TS}"
    return words


def _backend_run_statuses() -> set[str]:
    """`BACKEND_RUN_STATUSES` in run-status.ts, parsed from its source."""
    text = _frontend_file(_RUN_STATUS_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition(
        "export const BACKEND_RUN_STATUSES: ReadonlySet<string> = new Set(["
    )
    assert sep, f"BACKEND_RUN_STATUSES not found in {_RUN_STATUS_TS}"
    body, sep, _ = after.partition("]")
    assert sep, f"BACKEND_RUN_STATUSES not terminated in {_RUN_STATUS_TS}"
    words = set(_ARRAY_ITEM_RE.findall(body))
    assert words, f"parsed no statuses out of BACKEND_RUN_STATUSES in {_RUN_STATUS_TS}"
    return words


def _test_vocabulary(name: str) -> set[str]:
    """One `const <name> = [...]` array of status words in status.test.ts."""
    text = _frontend_file(_STATUS_TEST_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition(f"const {name} = [")
    assert sep, f"{name} not found in {_STATUS_TEST_TS}"
    body, sep, _ = after.partition("]")
    assert sep, f"{name} array not terminated in {_STATUS_TEST_TS}"
    words = set(_ARRAY_ITEM_RE.findall(body))
    assert words, f"parsed no status words out of {name}"
    return words


def _run_statuses() -> set[str]:
    return set(typing.get_args(RunStatus))


def _loop_statuses() -> set[str]:
    return set(typing.get_args(LoopStatus))


def _literal_words(node: ast.expr) -> set[str]:
    """The string literals one keyword value can evaluate to.

    A plain literal, or a conditional choosing between two of them — the forms
    ``_artifact_dto`` uses. Anything else is a computed word no static anchor
    can see, so it fails loudly instead of narrowing the check in silence.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_words(node.body) | _literal_words(node.orelse)
    raise AssertionError(
        f"_artifact_dto words publication with {ast.unparse(node)!r}, which this "
        "parity check cannot read — keep it a literal or a choice between two."
    )


def _publication_states() -> set[str]:
    """Every word ``_artifact_dto`` can put in ``WorkArtifactDTO.publication``.

    The field is a bare ``str`` with no Literal to import, so the ``publication=``
    keywords of the builder's own syntax tree are the only authority there is.
    """
    states = {
        word
        for call in calls(definition(work_service._artifact_dto))
        for kw in call.keywords
        if kw.arg == "publication"
        for word in _literal_words(kw.value)
    }
    assert states, "parsed no publication states out of _artifact_dto"
    return states


def test_meaning_of_covers_every_driver_status() -> None:
    """Every epic-snapshot status has a mark of its own, not the todo fallback."""
    table = _meaning_of()
    missing = sorted(s.value for s in DriverStatus if s.value not in table)
    assert not missing, (
        f"{_STATUS_TS} has no mark for DriverStatus {missing} — the epic snapshot "
        "serves these and they would paint as the muted todo ring, identical to "
        "work nobody has started. Add each to MEANING_OF with the meaning it "
        "deserves, and to DRIVER_STATUSES in status.test.ts."
    )


def test_meaning_of_covers_every_run_status() -> None:
    """Every persisted run status has a mark of its own."""
    table = _meaning_of()
    missing = sorted(s for s in _run_statuses() if s not in table)
    assert not missing, (
        f"{_STATUS_TS} has no mark for RunStatus {missing} — add each to "
        "MEANING_OF and to RUN_STORE_STATUSES in status.test.ts."
    )


def test_meaning_of_covers_every_loop_status() -> None:
    """Every goal-loop status has a mark of its own.

    ``MEANING_OF`` learned the six ``LoopStatus`` words with only
    ``LOOP_STATUSES`` in ``status.test.ts`` transcribing them, so a seventh loop
    state added to ``run_store.py`` satisfied every test while painting the muted
    todo ring — "nothing has started" about a loop that is pursuing a goal, or
    one that stopped needing a human.
    """
    table = _meaning_of()
    missing = sorted(s for s in _loop_statuses() if s not in table)
    assert not missing, (
        f"{_STATUS_TS} has no mark for LoopStatus {missing} — the loops page "
        "serves these and they would paint as the muted todo ring. Add each to "
        "MEANING_OF and to LOOP_STATUSES in status.test.ts."
    )


@pytest.mark.policy
def test_meaning_of_covers_every_publication_state() -> None:
    """The publication field is mapped whole, not two words out of three.

    ``MEANING_OF`` held ``not_required`` and ``pending`` but not ``published``,
    so a work item whose pull request is open painted the same muted todo ring
    as one nobody has published — from the same three-valued field, half of it
    mapped.
    """
    table = _meaning_of()
    missing = sorted(s for s in _publication_states() if s not in table)
    assert not missing, (
        f"{_STATUS_TS} has no mark for WorkArtifactDTO.publication {missing} — "
        "add each to MEANING_OF and to PUBLICATION_STATES in status.test.ts."
    )


@pytest.mark.policy
def test_frontend_test_transcribes_the_whole_publication_field() -> None:
    """The frontend's transcription of the field is held to the DTO builder."""
    transcribed = _test_vocabulary("PUBLICATION_STATES")
    expected = _publication_states()
    assert transcribed == expected, (
        f"PUBLICATION_STATES in {_STATUS_TEST_TS} has drifted from "
        f"_artifact_dto: missing {sorted(expected - transcribed)}, "
        f"unknown {sorted(transcribed - expected)}"
    )


def test_frontend_test_transcribes_the_whole_driver_status_enum() -> None:
    """The frontend guard is anchored to the enum, not to a frontend subset.

    Without this, `DRIVER_STATUSES` can quietly shrink back to the eight words
    `STATUS_COLOR` colours and pass vacuously again.
    """
    transcribed = _test_vocabulary("DRIVER_STATUSES")
    expected = {s.value for s in DriverStatus}
    assert transcribed == expected, (
        f"DRIVER_STATUSES in {_STATUS_TEST_TS} has drifted from DriverStatus: "
        f"missing {sorted(expected - transcribed)}, "
        f"unknown {sorted(transcribed - expected)}"
    )


def test_frontend_test_transcribes_the_whole_run_status_literal() -> None:
    """Same anchor for `RunStatus`, whose stale six-word copy in kpi.ts's
    comment is what the first transcription was taken from."""
    transcribed = _test_vocabulary("RUN_STORE_STATUSES")
    expected = _run_statuses()
    assert transcribed == expected, (
        f"RUN_STORE_STATUSES in {_STATUS_TEST_TS} has drifted from RunStatus: "
        f"missing {sorted(expected - transcribed)}, "
        f"unknown {sorted(transcribed - expected)}"
    )


def test_the_consoles_backend_run_status_set_is_the_run_status_literal() -> None:
    """`BACKEND_RUN_STATUSES` is the set every fixture run status is held to.

    `frontend/src/lib/fixtures.test.ts` checks every committed fixture run
    status against it, which is only worth anything if the set itself is the
    enum. It is a hand transcription (the frontend cannot import a Python
    Literal), so this is its anchor — and the reason it needs one is #2377: the
    fixtures had drifted to `failed` and `running`, words the enum has never
    held, and nothing on either side said so.
    """
    declared = _backend_run_statuses()
    expected = _run_statuses()
    assert declared == expected, (
        f"BACKEND_RUN_STATUSES in {_RUN_STATUS_TS} has drifted from RunStatus: "
        f"missing {sorted(expected - declared)}, "
        f"unknown {sorted(declared - expected)}"
    )


def test_frontend_test_transcribes_the_whole_loop_status_literal() -> None:
    """Same anchor for `LoopStatus`, so `LOOP_STATUSES` cannot lag the Literal.

    Without this the coverage check above still passes when the transcription
    shrinks, and `pinsEveryServedWord` in `status.test.ts` then stops demanding a
    pinned mark for the dropped word.
    """
    transcribed = _test_vocabulary("LOOP_STATUSES")
    expected = _loop_statuses()
    assert transcribed == expected, (
        f"LOOP_STATUSES in {_STATUS_TEST_TS} has drifted from LoopStatus: "
        f"missing {sorted(expected - transcribed)}, "
        f"unknown {sorted(transcribed - expected)}"
    )


def test_run_vocabulary_overrides_only_refine_served_words() -> None:
    """`RUN_MEANING_OF` refines a served word's meaning; it never adds a word.

    A word only the override table holds would be a status neither enum check
    above covers, because both read `MEANING_OF` alone. Keeping the override a
    strict subset is what makes "the run vocabulary reads the same words, one of
    them differently" true rather than hopeful.
    """
    overrides = _run_meaning_of()
    table = _meaning_of()
    unknown = sorted(key for key in overrides if key not in table)
    assert not unknown, (
        f"RUN_MEANING_OF in {_STATUS_TS} maps words MEANING_OF does not have: "
        f"{unknown} — add each to MEANING_OF too, so the DriverStatus and "
        "RunStatus coverage checks apply to them."
    )


def test_every_meaning_of_entry_names_a_real_meaning() -> None:
    """A typo'd meaning would be a silent TypeScript-only failure here."""
    text = _frontend_file(_STATUS_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition("export type StatusMeaning =")
    assert sep, f"StatusMeaning union not found in {_STATUS_TS}"
    union, sep, _ = after.partition(";")
    assert sep, f"StatusMeaning union not terminated in {_STATUS_TS}"
    meanings = set(_ARRAY_ITEM_RE.findall(union))
    named = set(_meaning_of().values()) | set(_run_meaning_of().values())
    unknown = sorted(named - meanings)
    assert not unknown, (
        f"MEANING_OF/RUN_MEANING_OF name meanings that do not exist: {unknown}"
    )


def test_loop_gate_covers_every_loop_status() -> None:
    """Every goal-loop status has a verification reading of its own.

    ``loopGate`` falls back to "gate pending" for a word it does not know, which
    is right for a state kew has not shipped and wrong for one it has: the first
    draft of the loops page named only ``achieved`` and ``needs_human``, so
    ``unmet`` — in ``LOOP_TERMINAL``, so the page had already stopped polling —
    reported its verification as still in progress forever, beside a StatusMark
    on the same row reading cancelled.
    """
    table = _loop_gate()
    missing = sorted(s for s in _loop_statuses() if s not in table)
    assert not missing, (
        f"{_LOOPS_TS} has no gate reading for LoopStatus {missing} — the loops "
        "page would print 'gate pending' for these forever. Add each to "
        "LOOP_GATE and to LOOP_STATUSES in loops.test.ts."
    )


def test_no_terminal_loop_status_reads_as_pending() -> None:
    """The invariant behind the table: a stopped poll cannot leave a pending gate.

    ``LOOP_TERMINAL`` is what stops the 4s poll, so any reading derived from a
    terminal status is the LAST one the page will ever paint. Asserted here as
    well as in ``loops.test.ts`` because this module is the one that knows the
    Python ``LoopStatus`` literal the two tables are supposed to cover.
    """
    table = _loop_gate()
    terminal = _loop_terminal()
    unknown = sorted(terminal - set(typing.get_args(LoopStatus)))
    assert not unknown, (
        f"LOOP_TERMINAL in {_LOOPS_TS} names non-LoopStatus words: {unknown}"
    )
    pending = sorted(s for s in terminal if table.get(s, "pending") == "pending")
    assert not pending, (
        f"{_LOOPS_TS} reads terminal LoopStatus {pending} as a pending gate — the "
        "poll stops on these, so the reading can never be corrected."
    )


def test_loop_terminal_excludes_every_resumable_status() -> None:
    """A loop an operator can restart must not stop the loops page's poll.

    ``LOOP_TERMINAL`` arms and disarms the 4s poll on the loops page, and its
    docstring says the exclusion rule is resumability — but nothing held it to
    the frozenset that actually decides resumability. ``budget_limited`` was
    listed as terminal while ``kew loop resume --budget`` bumps the cap and puts
    the loop straight back into ``pursuing``, so the page permanently disarmed
    its poll for a running loop and the row read "held" until a manual reload.
    """
    terminal = _loop_terminal()
    resumable = terminal & _RESUMABLE
    assert not resumable, (
        f"LOOP_TERMINAL in {_LOOPS_TS} calls {sorted(resumable)} terminal, but "
        "_RESUMABLE in kew/loops/delegate.py lets an operator restart them — the "
        "loops page stops its 4s poll on a terminal status, so the resume "
        "transition back to 'pursuing' would never be painted. Remove each from "
        "LOOP_TERMINAL and from its expected set in loops.test.ts."
    )


def test_every_unresumable_finished_loop_status_stops_the_poll() -> None:
    """The other half: a genuinely finished loop must not poll forever.

    ``pursuing`` is the only live state; everything else is either resumable or
    over. Without this, the fix for the check above could be "empty the set",
    which trades a frozen row for a poll that never stops.
    """
    finished = _loop_statuses() - _RESUMABLE - {"pursuing"}
    assert finished, "LoopStatus has no finished-and-unresumable state left to check"
    missing = sorted(finished - _loop_terminal())
    assert not missing, (
        f"{_LOOPS_TS} keeps polling LoopStatus {missing} — they are finished and "
        "not in _RESUMABLE, so the 4s poll would tick forever with no transition "
        "left to catch. Add each to LOOP_TERMINAL and to loops.test.ts."
    )


def test_loop_test_transcribes_the_whole_resumable_frozenset() -> None:
    """`RESUMABLE` in loops.test.ts cannot lag `_RESUMABLE` either.

    The frontend cannot import the frozenset, so its own guard iterates a copy.
    A copy that drops a word stops guarding it, which is exactly how
    ``budget_limited`` reached ``LOOP_TERMINAL`` in the first place.
    """
    text = _frontend_file(_LOOPS_TEST_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition("const RESUMABLE = [")
    assert sep, f"RESUMABLE not found in {_LOOPS_TEST_TS}"
    body, sep, _ = after.partition("]")
    assert sep, f"RESUMABLE array not terminated in {_LOOPS_TEST_TS}"
    transcribed = set(_ARRAY_ITEM_RE.findall(body))
    assert transcribed == set(_RESUMABLE), (
        f"RESUMABLE in {_LOOPS_TEST_TS} has drifted from _RESUMABLE: "
        f"missing {sorted(_RESUMABLE - transcribed)}, "
        f"unknown {sorted(transcribed - _RESUMABLE)}"
    )


def test_loop_test_transcribes_the_whole_loop_status_literal() -> None:
    """`LOOP_STATUSES` in loops.test.ts cannot lag the Literal either.

    Its per-status coverage check iterates that array, so a shrunken
    transcription stops demanding a reading for the dropped word.
    """
    text = _frontend_file(_LOOPS_TEST_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition("const LOOP_STATUSES = [")
    assert sep, f"LOOP_STATUSES not found in {_LOOPS_TEST_TS}"
    body, sep, _ = after.partition("]")
    assert sep, f"LOOP_STATUSES array not terminated in {_LOOPS_TEST_TS}"
    transcribed = set(_ARRAY_ITEM_RE.findall(body))
    expected = _loop_statuses()
    assert transcribed == expected, (
        f"LOOP_STATUSES in {_LOOPS_TEST_TS} has drifted from LoopStatus: "
        f"missing {sorted(expected - transcribed)}, "
        f"unknown {sorted(transcribed - expected)}"
    )


# ---------------------------------------------------------------------------
# The committed fixture set, held to the readings the host DERIVES (#2377).
#
# A status word is not the only thing a fixture can carry that the backend
# cannot produce. These are anchored here because each is computed, not chosen,
# and the frontend has no way to import any of them:
#
# * the in-flight verdict's wording, which `_rule_in_flight` builds out of the
#   execution dimension's own `run_status` — so a fixture that changes the
#   status and leaves the sentence standing serves a view where the two
#   disagree. #2377 changed `run_status` to `pending` and left "run running"
#   in the derived detail;
# * `PENDING_RUN_MAX_AGE_S`, the bound past which a pending row is no longer a
#   run kew is executing. `fixtures.test.ts` holds every in-flight fixture row
#   inside it, which is worth something only if the number it holds them to is
#   this one. The rows that needed it were served 8d23h old, which is a fixture
#   claiming "2 working" about two runs the host reads as abandoned;
# * a work view's execution dimension, which `_execution_dto` reads off
#   `facts.latest` — the newest row the ledger holds on that item. Correcting
#   the two in-flight start times moved the projection's `composed_at` PAST
#   item 2055's pending review round, so the view beside it reported a
#   nine-day-old success the host could no longer have read as latest;
# * `fleet.pending`, which is `len(store.list_pending())`. `0` was true of a
#   ledger whose live rows sat at `running`, a status that query does not
#   select, and silently false once they became `pending`. Three payloads carry
#   that one reading, each read at the instant it states (or, where it states
#   none, at the epoch the set is served as), and all three are held.
#
# The last two are read against the committed ledger itself rather than against
# a constant, so both need the real records: `_fixture_ledger` builds them and
# the composer's own `_attempt_order` decides which is newest.
# ---------------------------------------------------------------------------

_FIXTURES_TEST_TS = "frontend/src/lib/fixtures.test.ts"
_PLUGIN_TS = "frontend/fixtures/plugin.ts"
# The committed run ledger every other payload in the set is a reading OF.
_RUNS_JSON = "runs.json"

# `const PENDING_RUN_MAX_AGE_MS = 4 * 60 * 60 * 1000;` — a product of integers,
# so the mirror stays readable as the bound it is rather than as a magic total.
_TS_MS_RE = re.compile(
    r"const PENDING_RUN_MAX_AGE_MS = ([\d_]+(?:\s*\*\s*[\d_]+)*);",
)

# `export const FIXTURE_EPOCH = '2026-10-03T14:48:00+00:00';` — the instant the
# whole set is served AS, and so the instant a payload stating none of its own
# was read at.
_EPOCH_RE = re.compile(r"export const FIXTURE_EPOCH = '([^']+)';")

# Every committed reading of `fleet_status(store).pending`, as the
# `<payload><path>` keys `_fleet_readings` builds. A floor, not a whitelist:
# a payload that grows the reading is checked by arriving, and this only keeps
# the check from passing because it found nowhere to look.
_FLEET_READINGS = frozenset(
    {
        "fleet-status.json",
        "dashboard.json.glance.fleet",
        "health.json.fleet",
    }
)


def _template(node: ast.expr) -> str:
    """An f-string as a ``str.format`` template over the one value it reads.

    Each interpolation becomes ``{status}`` whatever expression fills it, so the
    template is the wording ``_rule_in_flight`` chose, not the name of a local.
    """
    assert isinstance(node, ast.JoinedStr), (
        f"_rule_in_flight words its verdict with {ast.unparse(node)!r}, not an "
        "f-string over the run status"
    )
    parts = [
        part.value if isinstance(part, ast.Constant) else "{status}"
        for part in node.values
    ]
    assert parts.count("{status}") == 1, (
        f"{ast.unparse(node)!r} interpolates something besides the run status"
    )
    return "".join(parts)


def _in_flight_templates() -> list[str]:
    """How `_rule_in_flight` words its verdict, read off its syntax tree: the
    exception detail, then the safest action's reason."""
    verdicts = [
        call
        for call in calls(definition(work_service._rule_in_flight))
        if dotted(call.func) == "_verdict"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "in_flight"
    ]
    assert len(verdicts) == 1, (
        f"expected one in_flight verdict in _rule_in_flight, parsed {len(verdicts)}"
    )
    (verdict,) = verdicts
    reason = next((kw.value for kw in verdict.keywords if kw.arg == "reason"), None)
    assert len(verdict.args) >= 2 and reason is not None, (
        "expected _rule_in_flight to word its detail and its reason from run_status"
    )
    return [_template(verdict.args[1]), _template(reason)]


def _fixture_payloads() -> list[tuple[str, object]]:
    """Every committed fixture payload, as `(file name, parsed JSON)`.

    Both directories: a scenario payload is served in place of a committed one
    for a single shot, so it is read by the same surfaces under the same rules.
    """
    root = _frontend_file(_PLUGIN_TS).parent
    paths = sorted(root.glob("factory/*.json")) + sorted(root.glob("scenarios/*.json"))
    assert paths, f"no fixture payloads found under {root}"
    return [(path.name, json.loads(path.read_text(encoding="utf-8"))) for path in paths]


def _fixture_ledger() -> list[RunRecord]:
    """The committed run ledger — ``factory/runs.json`` as the host's own rows.

    Built into real :class:`RunRecord`s so the checks below can read them with
    the composer's own functions rather than with a second opinion about which
    attempt is newest or what an execution dimension says.
    """
    rows = dict(_fixture_payloads())[_RUNS_JSON]
    assert isinstance(rows, list) and rows, f"{_RUNS_JSON} holds no run rows"
    return [_as_record(row) for row in rows]


def _as_record(row: dict[str, object]) -> RunRecord:
    """One committed run row as the record ``service.work`` would read.

    Only the fields the execution dimension is composed from; everything else
    on the row is another surface's business and takes its dataclass default.
    """
    return RunRecord(
        run_id=str(row["run_id"]),
        issue_num=int(str(row["issue_num"])),
        branch=str(row.get("branch") or ""),
        status=str(row["status"]),  # type: ignore[arg-type]
        exit_code=0,
        started_at=_instant(row.get("started_at")),  # type: ignore[arg-type]
        finished_at=_instant(row.get("finished_at")),
        error_category=str(row.get("error_category") or ""),
        attempt_number=int(str(row.get("attempt_number") or 1)),
        item_id=str(row.get("item_id") or row["issue_num"]),
        run_kind=str(row.get("run_kind") or "dispatch"),
        pr_url=str(row["pr_url"]) if row.get("pr_url") else None,
    )


def _instant(value: object) -> datetime | None:
    """An ISO instant off a fixture row, or None where the row holds none."""
    return datetime.fromisoformat(str(value)) if value else None


def _visible_at(
    ledger: list[RunRecord], item_id: str, moment: datetime
) -> list[RunRecord]:
    """Every *ledger* row on *item_id* that had started by *moment*, newest first.

    Ordered by ``_attempt_order``, which is what ``_snapshot_facts`` orders the
    snapshot's own ``runs`` tuple by — so "the latest run" here is the record
    ``WorkFacts.latest`` would hand the composer, not a guess resembling it.
    """
    visible = [
        record
        for record in ledger
        if record.item_id == item_id
        and record.started_at is not None
        and record.started_at <= moment
    ]
    return sorted(visible, key=work_service._attempt_order, reverse=True)


def _fixture_epoch() -> datetime:
    """``FIXTURE_EPOCH`` in plugin.ts — the instant the whole set is read as.

    Fixture mode serves the set under ONE delta, ``now - FIXTURE_EPOCH``, so a
    payload carrying no instant of its own is served as of the epoch. Read off
    the plugin's source rather than restated here: ``fixtures.test.ts`` already
    pins it to the newest instant in the committed set.
    """
    text = _frontend_file(_PLUGIN_TS).read_text(encoding="utf-8")
    match = _EPOCH_RE.search(text)
    assert match, f"FIXTURE_EPOCH not found in {_PLUGIN_TS}"
    epoch = _instant(match.group(1))
    assert epoch is not None
    return epoch


def _read_at(payload: dict[str, object], epoch: datetime) -> tuple[datetime, str]:
    """When *payload* says it read the run table, and how it said so.

    ``as_of.runs`` is a projection's own word for that instant and
    ``checked_at`` is a health report's. ``/api/fleet/status`` is a latch and a
    count with no timestamp anywhere on it, so it states nothing and is served
    as now — which under the set's single delta is *epoch*.
    """
    as_of = payload.get("as_of")
    if isinstance(as_of, dict) and (stated := _instant(as_of.get("runs"))):
        return stated, f"as_of.runs {as_of['runs']}"
    if checked := _instant(payload.get("checked_at")):
        return checked, f"checked_at {payload['checked_at']}"
    return epoch, f"FIXTURE_EPOCH {epoch.isoformat()} (the payload states none)"


def _fleet_readings(payload: object) -> list[tuple[str, dict[str, object]]]:
    """Every ``fleet_status`` reading in *payload*, as `(path, reading)`.

    Recognised by the shape :class:`FleetStatusDTO` serialises to — ``pending``
    beside ``stopped`` — rather than per file, so the root of
    ``/api/fleet/status``, a dashboard's ``glance.fleet`` and the health
    report's ``fleet`` (same two fields plus the latch's ``reason``) are all
    found, and a payload that grows the reading is covered by arriving.
    """
    found: list[tuple[str, dict[str, object]]] = []

    def walk(value: object, where: str) -> None:
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{where}[{i}]")
            return
        if not isinstance(value, dict):
            return
        if "pending" in value and "stopped" in value:
            found.append((where, value))
        for key, item in value.items():
            walk(item, f"{where}.{key}")

    walk(payload, "")
    return found


def _fixture_work_views() -> list[tuple[str, dict[str, object]]]:
    """Every work view in the committed fixture set, as `(where, view)`.

    Recognised by the pairing `compose_work_view` emits — an ``execution``
    dimension beside an ``exception`` one — rather than per file, so a payload
    that grows a view is covered by arriving.
    """
    found: list[tuple[str, dict[str, object]]] = []

    def walk(value: object, where: str) -> None:
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{where}[{i}]")
            return
        if not isinstance(value, dict):
            return
        execution = value.get("execution")
        exception = value.get("exception")
        if isinstance(execution, dict) and isinstance(exception, dict):
            found.append((where, value))
        for key, item in value.items():
            walk(item, f"{where}.{key}")

    for name, payload in _fixture_payloads():
        walk(payload, name)
    return found


@pytest.mark.policy
def test_the_fixtures_word_an_in_flight_view_as_the_host_derives_it() -> None:
    """A fixture in-flight view says what ``_rule_in_flight`` would say.

    Both sentences are built from the execution dimension's ``run_status``, so
    neither can be edited away from it: #2377 moved the fixture's in-flight run
    off the display-only ``running`` and onto ``pending`` while the derived
    detail went on reading "run running" — the removed word surviving in the
    one field that is not a free-text caption.

    Only the ``in_flight`` exception state is held to this. ``review_in_flight``
    is a different verdict with wording of its own, and the other states carry
    captions a person wrote.
    """
    detail_template, reason_template = _in_flight_templates()
    views = [
        (where, view)
        for where, view in _fixture_work_views()
        if (view["exception"] or {}).get("state") == "in_flight"  # type: ignore[union-attr]
    ]
    assert views, (
        "no in-flight work view in the fixture set — this check guards the "
        "wording of one, so it must not pass by finding none"
    )
    for where, view in views:
        execution = view["execution"]
        exception = view["exception"]
        assert isinstance(execution, dict) and isinstance(exception, dict)
        status = execution["run_status"]
        assert exception["detail"] == detail_template.format(status=status), (
            f"{where}: exception.detail is {exception['detail']!r}, but "
            f"_rule_in_flight derives it from run_status={status!r} as "
            f"{detail_template.format(status=status)!r}"
        )
        reason = view["safest_action"]
        assert isinstance(reason, dict)
        assert reason["reason"] == reason_template.format(status=status), (
            f"{where}: safest_action.reason is {reason['reason']!r}, but "
            f"_rule_in_flight derives it from run_status={status!r} as "
            f"{reason_template.format(status=status)!r}"
        )


def test_the_fixture_tests_pending_bound_is_the_hosts_own() -> None:
    """The TypeScript mirror of ``PENDING_RUN_MAX_AGE_S`` cannot drift from it.

    ``fixtures.test.ts`` holds every in-flight fixture row inside that bound,
    because a pending row older than it is one the host reads as a dead host
    rather than as work in progress — and the fixture would then claim "2
    working" about two abandoned runs. The frontend cannot import the constant,
    so this is its anchor.
    """
    text = _frontend_file(_FIXTURES_TEST_TS).read_text(encoding="utf-8")
    expression = _TS_MS_RE.search(text)
    assert expression, f"PENDING_RUN_MAX_AGE_MS not found in {_FIXTURES_TEST_TS}"
    mirrored = 1
    for factor in expression.group(1).split("*"):
        mirrored *= int(factor.strip().replace("_", ""))
    assert mirrored == PENDING_RUN_MAX_AGE_S * 1000, (
        f"PENDING_RUN_MAX_AGE_MS in {_FIXTURES_TEST_TS} is {mirrored}ms, but "
        f"PENDING_RUN_MAX_AGE_S is {PENDING_RUN_MAX_AGE_S}s — the fixture "
        "guard would hold in-flight rows to a bound the host does not use."
    )


def test_the_fixtures_compose_each_work_view_off_its_own_latest_run() -> None:
    """A fixture work view reports the run its own ledger makes LATEST.

    ``_execution_dto`` reads ``facts.latest``, and ``_snapshot_facts`` fills
    ``runs`` with every row on the item — a review round included — ordered by
    ``_attempt_order``. So a view's execution dimension is not a reading a
    fixture may choose: it is the newest ledger row on that item as of the
    instant the view says it was ``composed_at``.

    That is what broke here. #2377 gave the two in-flight rows the start times
    the ledger really holds, and the projection's ``composed_at`` moved with
    them to 13m50s AFTER item 2055's pending review round had started — while
    the view beside it went on serving ``completed / success`` off the
    nine-day-old implementation run. The host composing at that instant would
    have read the review row as latest and emitted ``in_flight / pending``
    (reviewer finding, PR #2441).

    A row that starts AFTER the view was composed is no contradiction: the set
    is a collection of captures, each taken at its own instant (the pipeline
    and evidence payloads for the same item are nine days behind this
    projection), and a capture cannot have read a row that did not yet exist.
    The rule is only that a capture must reflect every row that DID.
    """
    ledger = _fixture_ledger()
    checked: list[str] = []
    for where, view in _fixture_work_views():
        composed = _instant(view["composed_at"])
        assert composed is not None, f"{where}: no composed_at to read it against"
        identity = view["identity"]
        assert isinstance(identity, dict)
        item_id = str(identity["item_id"])
        visible = _visible_at(ledger, item_id, composed)
        if not visible:
            # The ledger payload is one PAGE of the runs table, so a view can
            # name an attempt it does not carry (SFD-229's does). Nothing to
            # hold the view to, rather than a licence to hold it to nothing.
            continue
        checked.append(where)
        latest = visible[0]
        assert identity["run_id"] == latest.run_id, (
            f"{where}: identity.run_id is {identity['run_id']!r}, but the "
            f"newest row on {item_id} at {view['composed_at']} is "
            f"{latest.run_id!r} ({latest.status}, started {latest.started_at}) "
            "— the host reads that one as facts.latest"
        )
        expected = work_service._execution_dto(
            work_service.WorkFacts(
                item_id=item_id,
                identity=None,
                runs=tuple(visible),
                orphan_prs=(),
                pull_request=work_service._unobserved_pr(),
                tracker_item=work_service._unobserved_item(),
                as_of=composed,
            )
        )
        execution = view["execution"]
        assert isinstance(execution, dict)
        for field in ("state", "run_status", "started_at", "finished_at"):
            assert execution[field] == getattr(expected, field), (
                f"{where}: execution.{field} is {execution[field]!r}, but "
                f"_execution_dto off the ledger at {view['composed_at']} "
                f"derives {getattr(expected, field)!r}"
            )
        # ``attempts`` is ``len(facts.runs)`` over the WHOLE table, so the page
        # can only bound it from below (#2053's first attempt is off it).
        assert int(str(execution["attempts"])) >= len(visible), (
            f"{where}: execution.attempts is {execution['attempts']}, fewer "
            f"than the {len(visible)} rows {_RUNS_JSON} already shows on "
            f"{item_id} by {view['composed_at']}"
        )
    assert len(checked) >= 6, (
        "the committed set's work views are not being held to its ledger — "
        f"only {checked} had a run row to read, which is too few to be the "
        "projection plus its single-item read"
    )


def test_the_fixtures_count_the_pending_rows_their_own_ledger_holds() -> None:
    """``fleet.pending`` is a COUNT, not a caption — in every payload holding it.

    ``fleet_status`` fills it with ``len(store.list_pending())`` — every row at
    ``status == 'pending'`` — so a capture cannot disagree with the ledger it
    was taken from. Pending rows are the newest rows there are and ``runs.json``
    is served newest-first, so none of them can be off that page.

    #2377 moved the two in-flight rows off the display-only ``running`` and
    onto ``pending``, which took this count from right to wrong without
    touching it: ``0`` was true of a ledger whose live rows were at a status
    ``list_pending`` does not select.

    Three committed payloads carry that one reading — the root of
    ``/api/fleet/status``, a dashboard's ``glance.fleet`` and the health
    report's ``fleet`` — and the first round of this guard read only the
    dashboards, so the other two stayed unheld while it passed (reviewer
    finding, PR #2441). They are found by SHAPE here, and the floor below names
    all three, so neither a payload that grows the reading nor one that loses
    its ``as_of`` can slip out of the check.
    """
    ledger = _fixture_ledger()
    undated = [
        record.run_id
        for record in ledger
        if record.status == "pending" and record.started_at is None
    ]
    assert not undated, (
        f"{_RUNS_JSON} holds undated pending rows ({undated}): "
        "``list_pending`` counts a row whatever its start time, so there is no "
        "instant at which a capture may leave one out"
    )
    epoch = _fixture_epoch()
    checked: list[str] = []
    for name, payload in _fixture_payloads():
        if not isinstance(payload, dict):
            continue
        read_at, stated = _read_at(payload, epoch)
        pending = [
            record
            for record in ledger
            if record.status == "pending"
            and record.started_at is not None
            and record.started_at <= read_at
        ]
        for where, fleet in _fleet_readings(payload):
            checked.append(f"{name}{where}")
            assert fleet["pending"] == len(pending), (
                f"{name}{where}.pending is {fleet['pending']}, but "
                f"{_RUNS_JSON} holds {len(pending)} pending rows "
                f"({[record.run_id for record in pending]}) by {stated}, the "
                "instant this payload was read at"
            )
    assert set(checked) >= _FLEET_READINGS, (
        "the committed readings of fleet_status(store).pending are not all "
        f"being held to {_RUNS_JSON} — this round found {sorted(checked)}, "
        f"missing {sorted(_FLEET_READINGS.difference(checked))}. A reading "
        "that moved belongs in the floor under its new name; one that was "
        "deleted belongs out of it"
    )
