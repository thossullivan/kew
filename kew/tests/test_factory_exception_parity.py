"""The factory UI decides a state word for every exception the projection raises.

:func:`kew.service.work.derive_verdict` names the ONE exception every surface
reads, and two frontend tables are keyed on that vocabulary:

* ``EXCEPTION_LABEL`` in ``frontend/src/lib/dashboard.ts`` — its display name,
  exported as ``KNOWN_EXCEPTION_STATES``;
* ``EXCEPTION_STATE_WORD`` in ``frontend/src/lib/work/list.ts`` — the Work list's
  interim state word, which the exception overrides the raw execution state with.

``derive_verdict`` also names the ONE safest action behind every verdict, and a
third frontend table is keyed on that vocabulary:

* ``SAFEST_ACTION_ACTOR`` in ``frontend/src/lib/work/evidence.ts`` — whether the
  next move is a person's, which is what raises the Work screen's needs-you band
  (FUI-21). An action that table does not hold reads as kew's own move and raises
  no band at all, so that silence is what the last three tests here catch.

Both tables tolerate a state they do not hold, and that tolerance is the bug this
module exists to catch. A missing label still renders (spaced, verbatim), which
is honest. A missing *state word* is not: ``itemState`` falls through to the
execution state, so an item whose artifact a human closed without merging —
``declined``, execution ``completed`` — painted as **success**, the one reading
that says there is nothing left to do about abandoned work.

``list.test.ts`` did hold ``EXCEPTION_STATE_WORD`` to
``KNOWN_EXCEPTION_STATES``, but both are client-side transcriptions of the same
Python vocabulary, so a state missing from BOTH passed: the guard compared one
copy to the other, and ``declined`` was absent from each. This module is the
missing anchor, read from ``derive_verdict``'s own source.

A fourth table has the same shape and the same trap (#2246):

* ``ATTENTION_STATE_WORD`` in ``frontend/src/lib/work/list.ts`` — the State
  column's word for a needs-you ROW, keyed on ``DashboardRow.attention_kind``.
  That vocabulary is :class:`kew.waits.AttentionKind` plus the three kinds the
  work projection and the approval store name for themselves, and its fallback
  is ``manual``: the word every entry row used to read, which on the live screen
  was five of seven rows saying one thing about five different waits.

Nothing here asserts WHICH word a state gets — that is a design decision
``list.test.ts`` pins. This module only asserts that the decision was made.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from support.source_ast import calls, dotted, module_tree

from kew.service import attention as attention_service
from kew.service import needs_you as needs_you_service
from kew.service import work as work_service
from kew.waits import AttentionKind

# Every check here reads the projection's vocabulary off its syntax tree: the
# frontend tables and derive_verdict are one rule, kept in sync (#2491).
pytestmark = pytest.mark.policy

_DASHBOARD_TS = "frontend/src/lib/dashboard.ts"
_LIST_TS = "frontend/src/lib/work/list.ts"
_EVIDENCE_TS = "frontend/src/lib/work/evidence.ts"

# A `key: 'value',` entry of a `Record<string, string>` table. Unquoted keys
# only — every entry in both tables is a bare identifier, so a quoted key is a
# style break worth failing on rather than a parse gap to tolerate.
_ENTRY_RE = re.compile(r"^\s{2}([a-z][a-z0-9_]*):\s*'([^']*)',?\s*$", re.MULTILINE)


def _frontend_file(relative: str) -> Path:
    """Locate a frontend source file by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} not present in this checkout")


def _ts_table(relative: str, declaration: str) -> dict[str, str]:
    """One `Record<string, string>` object literal, parsed from its source."""
    text = _frontend_file(relative).read_text(encoding="utf-8")
    _, sep, after = text.partition(declaration)
    assert sep, f"{declaration} not found in {relative}"
    body, sep, _ = after.partition("\n};")
    assert sep, f"{declaration} is not a terminated object literal in {relative}"
    table = dict(_ENTRY_RE.findall(body))
    assert table, f"parsed no entries out of {declaration} in {relative}"
    return table


def _exception_labels() -> dict[str, str]:
    return _ts_table(_DASHBOARD_TS, "const EXCEPTION_LABEL: Record<string, string> = {")


def _state_words() -> dict[str, str]:
    return _ts_table(
        _LIST_TS, "export const EXCEPTION_STATE_WORD: Record<string, string> = {"
    )


def _string_literals(node: ast.expr) -> set[str]:
    """The string values one argument expression can evaluate to.

    Only the two forms the verdict table actually uses: a plain literal, and the
    conditional in ``_rule_terminal`` that picks between two of them. Anything
    else is a computed exception name, which would defeat a static anchor — so
    it fails loudly here instead of silently narrowing what this test covers.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _string_literals(node.body) | _string_literals(node.orelse)
    raise AssertionError(
        "a _verdict() call names its exception with something other than a string "
        f"literal ({ast.dump(node)[:120]}) — this parity check can no longer see "
        "the whole vocabulary. Keep the name a literal, or teach it the new form."
    )


def _exception_states() -> set[str]:
    """Every exception state ``kew.service.work`` can put in a verdict.

    Read from the module's AST rather than by exercising the rules: a verdict
    needs a composed ``_Derivation`` for facts nobody has, and a rule that no
    fixture happens to reach is exactly the one whose state goes unmapped.
    ``WorkExceptionDTO`` is constructed in one place — ``_verdict`` — so the
    first argument of every ``_verdict(...)`` call is the whole vocabulary.
    """
    states: set[str] = set()
    for node in ast.walk(module_tree(work_service)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_verdict"
        ):
            assert node.args, (
                f"a _verdict() call names no exception (line {node.lineno})"
            )
            states |= _string_literals(node.args[0])
    assert states, "parsed no exception states out of kew.service.work"
    return states


def _construction_sites(name: str) -> list[str]:
    """The function each call constructing *name* in ``kew.service.work`` sits in.

    Read off the tree, so a wrapped call, an aliased module or a call written
    ``work.WorkExceptionDTO(...)`` all count, while a mention in a comment, a
    docstring or a type annotation does not. A call outside every function is
    ``<module>``.
    """
    tree = module_tree(work_service)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    sites: list[str] = []
    for call in calls(tree):
        if (dotted(call.func) or "").split(".")[-1] != name:
            continue
        # The innermost function holding the call is the last one to hold it,
        # since ast.walk visits an enclosing def before the defs nested in it.
        holders = [fn.name for fn in functions if call in ast.walk(fn)]
        sites.append(holders[-1] if holders else "<module>")
    return sites


def test_verdicts_are_built_in_one_place() -> None:
    """The AST read above is only the whole vocabulary while that stays true."""
    assert _construction_sites("WorkExceptionDTO") == ["_verdict"], (
        "kew.service.work constructs WorkExceptionDTO somewhere other than "
        "_verdict() — this module reads _verdict() calls only, so a second "
        "construction site is a state no parity check can see. Route it through "
        "_verdict(), or teach _exception_states() the new site."
    )


def test_every_exception_has_a_display_name() -> None:
    """A state with no label renders verbatim — honest, but never decided."""
    labels = _exception_labels()
    missing = sorted(state for state in _exception_states() if state not in labels)
    assert not missing, (
        f"derive_verdict can raise {missing}, which EXCEPTION_LABEL in "
        f"{_DASHBOARD_TS} does not name — the console would print the raw state. "
        "Add each with the name a reader should see."
    )


def test_every_exception_has_a_work_list_state_word() -> None:
    """A state with no word is painted as its bare execution state instead.

    This is the ``declined`` bug: the exception outranks the execution state
    precisely so a completed run is not "success" while a human's decision is
    outstanding, and a state absent from the table gets that override skipped.
    """
    words = _state_words()
    missing = sorted(state for state in _exception_states() if state not in words)
    assert not missing, (
        f"derive_verdict can raise {missing}, which EXCEPTION_STATE_WORD in "
        f"{_LIST_TS} does not map — itemState() would fall through to the raw "
        "execution state and paint the item as whatever its run did, ignoring the "
        "exception. Add each with the word it is waiting for."
    )


def test_the_frontend_invents_no_exception_the_projection_cannot_raise() -> None:
    """A dead key maps nothing and reads, forever, as though it were guarded."""
    served = _exception_states()
    for relative, table in (
        (_DASHBOARD_TS, _exception_labels()),
        (_LIST_TS, _state_words()),
    ):
        unknown = sorted(state for state in table if state not in served)
        assert not unknown, (
            f"{relative} maps exception states {unknown}, which derive_verdict "
            "never raises — a renamed or dropped exception leaves an entry that "
            "silently stops covering anything."
        )


def _safest_actions() -> set[str]:
    """Every safest action ``kew.service.work`` can recommend.

    Read the same way as the exception states, and for the same reason: a rule
    no fixture happens to reach is exactly the one whose action nobody
    classified. ``SafestActionDTO`` is constructed in one place — ``_verdict`` —
    so the ``action=`` of every ``_verdict(...)`` call is the whole vocabulary.
    """
    actions: set[str] = set()
    for node in ast.walk(module_tree(work_service)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_verdict"
        ):
            continue
        named = [kw for kw in node.keywords if kw.arg == "action"]
        assert named, (
            f"a _verdict() call names no safest action (line {node.lineno}) — "
            "this parity check reads the action= keyword, so a positional or "
            "computed one is a recommendation it can no longer see."
        )
        actions |= _string_literals(named[0].value)
    assert actions, "parsed no safest actions out of kew.service.work"
    return actions


def _safest_action_actors() -> dict[str, str]:
    return _ts_table(
        _EVIDENCE_TS, "export const SAFEST_ACTION_ACTOR: Record<string, string> = {"
    )


def test_safest_actions_are_built_in_one_place() -> None:
    """The AST read above is only the whole vocabulary while that stays true."""
    assert _construction_sites("SafestActionDTO") == ["_verdict"], (
        "kew.service.work constructs SafestActionDTO somewhere other than "
        "_verdict() — this module reads _verdict() calls only, so a second "
        "construction site is an action no parity check can see. Route it "
        "through _verdict(), or teach _safest_actions() the new site."
    )


def test_every_safest_action_names_who_acts_next() -> None:
    """An unclassified action silently stops raising the needs-you band.

    ``SAFEST_ACTION_ACTOR`` decides whether the Work screen paints its magenta
    band (FUI-21): ``person`` raises one, ``kew`` does not, and an action the
    table has never heard of reads as ``kew`` — deliberately, because the other
    default would spend the colour rationed to "a human is needed" on every
    action kew ever grows. That default is only safe while this test exists: a
    new human-waiting action missing from the table would otherwise leave the
    operator with no band, and nothing on screen saying there was one to show.
    """
    actors = _safest_action_actors()
    missing = sorted(action for action in _safest_actions() if action not in actors)
    assert not missing, (
        f"derive_verdict can recommend {missing}, which SAFEST_ACTION_ACTOR in "
        f"{_EVIDENCE_TS} does not classify — each reads as kew's own move, so no "
        "needs-you band would appear for it. Add each as 'person' (with a title "
        "in NEEDS_YOU_TITLE beside it) or as 'kew'."
    )
    named = sorted({actor for actor in actors.values()} - {"person", "kew"})
    assert not named, (
        f"SAFEST_ACTION_ACTOR in {_EVIDENCE_TS} names actors {named}; the band "
        "tests for 'person' alone, so anything else silently reads as kew's move."
    )


def test_the_frontend_invents_no_safest_action_nothing_recommends() -> None:
    """A dead key classifies nothing and reads as though it were covered."""
    served = _safest_actions()
    unknown = sorted(
        action for action in _safest_action_actors() if action not in served
    )
    assert not unknown, (
        f"{_EVIDENCE_TS} classifies safest actions {unknown}, which derive_verdict "
        "never recommends — a renamed or dropped action leaves an entry that "
        "silently stops covering anything."
    )


def _attention_words() -> dict[str, str]:
    return _ts_table(
        _LIST_TS, "export const ATTENTION_STATE_WORD: Record<string, string> = {"
    )


def _projection_kinds() -> set[str]:
    """The attention kinds the work projection names, which no enum holds.

    ``AttentionKind`` is the stream's own vocabulary; the needs-you set adds
    three of its own for rows that come off the work projection and the approval
    store — :func:`kew.service.attention.exception_kind`'s two returns, and the
    ``attention_kind="approval"`` the approval row is built with. They are read
    from the two modules' source for the same reason the exception vocabulary is:
    a renamed one has to fail here rather than leave a dead table entry.
    """
    kinds: set[str] = set()
    for module in (attention_service, needs_you_service):
        tree = module_tree(module)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "attention_kind"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                kinds.add(node.value.value)
        if module is attention_service:
            fn = next(
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "exception_kind"
            )
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    kinds.add(node.value.value)
    enum_values = {kind.value for kind in AttentionKind}
    extra = kinds - enum_values
    assert extra, (
        "parsed no projection-only attention kinds out of "
        "kew.service.attention / kew.service.needs_you"
    )
    return extra


def test_the_work_list_names_a_state_word_for_every_attention_kind() -> None:
    """Every kind a needs-you ROW can carry gets a word, not just a fallback.

    ``ATTENTION_STATE_WORD`` in ``frontend/src/lib/work/list.ts`` is the third
    table of this shape (#2246): a needs-you entry has no projection view, so
    neither table above can speak for its row, and this one decides the State
    column's word from ``DashboardRow.attention_kind``.

    Its fallback is ``manual`` — "a person is next, and nothing more is known" —
    which is honest for a kind this client has never heard of and wrong for one
    the server already distinguishes. Before #2246 every entry row read
    ``manual``, which on the live screen was five of seven rows saying the same
    word about five different waits, so a kind sliding back into that fallback is
    this defect returning one row at a time.
    """
    words = _attention_words()
    vocabulary = sorted({kind.value for kind in AttentionKind} | _projection_kinds())
    missing = [kind for kind in vocabulary if kind not in words]
    assert not missing, (
        f"a needs-you row can carry attention kinds {missing}, which "
        f"ATTENTION_STATE_WORD in {_LIST_TS} does not name — each reads as the "
        "'manual' fallback, the one word that says nothing about which wait it "
        "is. Give each the word for what it waits for."
    )


def test_the_work_list_invents_no_attention_kind_nothing_serves() -> None:
    """A dead key reads as covered while covering nothing."""
    vocabulary = {kind.value for kind in AttentionKind} | _projection_kinds()
    unknown = sorted(kind for kind in _attention_words() if kind not in vocabulary)
    assert not unknown, (
        f"ATTENTION_STATE_WORD in {_LIST_TS} names attention kinds {unknown}, "
        "which nothing serves — a renamed or dropped kind leaves an entry that "
        "silently stops covering anything."
    )


_CONSOLE_PROJECTION = "frontend/fixtures/factory/dashboard-work.json"
_CONSOLE_WORK_VIEW = "frontend/fixtures/factory/verbs-work-2055.json"


def _one_sentence_states() -> set[str]:
    """Exception states whose verdict puts ONE sentence in both of its fields.

    ``_verdict`` writes the exception's ``detail`` and the safest action's
    ``reason`` from two arguments, and for some verdicts they are the SAME
    sentence — ``_await_decision`` is one: an artifact awaiting a human decision
    has one thing to say, and it says it in both places.

    The console reads that: the Work header prints its exception line only where
    the needs-you block below it is not already carrying the sentence
    (``exceptionLine`` in ``frontend/src/lib/work/header.ts``, #2280 D10), and
    the block's own ``Why`` is whichever of the projection's two sentences its
    summary has not said. So a *fixture* that gives one of these states two
    different sentences puts the console in a state no served payload can reach
    — the containment test cannot fire, and ``/work`` draws one wait twice.

    Read off the source rather than written down, so a verdict that grows a
    second sentence one day relaxes this with it.
    """
    # Per state, whether each verdict that can raise it repeats itself. A state
    # several rules reach counts only where ALL of them do: `none` is raised
    # both with no detail at all and with a reason of its own, so one repeating
    # call must not speak for the rest.
    repeats: dict[str, list[bool]] = {}
    for node in ast.walk(module_tree(work_service)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_verdict"
        ):
            continue
        reason = next((kw for kw in node.keywords if kw.arg == "reason"), None)
        assert reason is not None, (
            f"a _verdict() call names no reason (line {node.lineno}) — this "
            "parity check reads the reason= keyword."
        )
        assert len(node.args) >= 2, (
            f"a _verdict() call names no detail (line {node.lineno})"
        )
        try:
            details = _string_literals(node.args[1])
            reasons = _string_literals(reason.value)
        except AssertionError:
            # A composed sentence (an f-string over the facts) is not one this
            # check can read, so the states it raises are recorded as not
            # repeating rather than being left out: a payload is held to this
            # only where every call that can produce it says one sentence.
            details, reasons = set(), {""}
        # Two empty strings are no sentence at all, which is not the invariant
        # this is about.
        said = details == reasons and details != {""}
        for state in _string_literals(node.args[0]):
            repeats.setdefault(state, []).append(said)
    same = {state for state, calls in repeats.items() if all(calls)}
    assert same, "parsed no single-sentence verdicts out of kew.service.work"
    return same


def test_the_committed_console_projection_repeats_what_the_server_repeats() -> None:
    """The browser's own fixtures say one wait in the number of sentences the
    server would say it in.

    The committed projection gave #2055 — ``awaiting_human_decision`` — one
    sentence in ``exception.detail`` and a different one in
    ``safest_action.reason``. No reachable server state does that, and the
    console's D10 containment gate is keyed on it, so ``/work?item=2055`` drew
    the header's exception line above a needs-you block saying something else:
    one wait, two sentences, on the surface D10 exists to clean up.
    """
    repeated = _one_sentence_states()
    # Not vacuous: the state the console's gate is about is one of them.
    assert "awaiting_human_decision" in repeated

    for relative in (_CONSOLE_PROJECTION, _CONSOLE_WORK_VIEW):
        payload = json.loads(_frontend_file(relative).read_text(encoding="utf-8"))
        views = payload.get("views", [payload])
        checked = 0
        for view in views:
            if view["exception"]["state"] not in repeated:
                continue
            checked += 1
            assert view["safest_action"]["reason"] == view["exception"]["detail"], (
                f"{relative}: the view for {view['identity']['item_id']} gives "
                f"{view['exception']['state']} two different sentences, which "
                "derive_verdict writes as one"
            )
        assert checked, f"{relative} exercises none of {sorted(repeated)}"
