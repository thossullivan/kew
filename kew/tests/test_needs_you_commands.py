"""A needs-you row carries its kind, its words and a command to run (#2244).

The go-live snapshot's SFD-232 row told an operator to "resume with
``kew epic resume go-live-2026-09 SFD-232``". There has never been a
``kew epic resume``. Beside it, every ``epic_holds`` row reached the console as
``budget_hold`` with ``primary_action: "approve"`` — a category that is
historical (the budget preflight was that table's first writer) and a button
that answers ``not_held`` on an epic hold — and the one real hint the payload
did carry, ``kew epic continue <name> <node>``, arrived with its placeholders
still in it.

So :class:`~kew.service.models.DashboardRowDTO` carries three more fields, and
this module holds them to the only things that make them worth printing:

* ``attention_kind`` is ``kew.waits``'s own kind, so a surface reads the
  category off the wait rather than off the historical row ``type``;
* ``label`` is the bucket's operator wording, owned in Python;
* ``command`` is runnable AS PRINTED — filled with this row's epic and item,
  and naming a subcommand the Typer app really has. The last part is checked by
  walking the app, because the failure this issue is named for is a sentence
  that reads like a command and is not one.

:class:`~kew.service.models.NodeAttentionDTO` carries ``command`` for the same
reason and under the same guard (#2250, the last section here): the epic DAG's
popover prints a command under a ``$`` prompt and a Copy button, and its first
round composed that command in the browser out of ``attention.action`` — which
is PROSE about the move, so every line it could produce was argument-less.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_service_dashboard import (
    FakeDriverState,
    FakeEpicTick,
    FakeRun,
    FakeStore,
    _config,
)
from typer.main import get_command

from kew.cli import app
from kew.epic_holds import (
    CI_PENDING_HOLD_REASON,
    CONTAINMENT_HOLD_PREFIX,
    REVIEW_BUDGET_HOLD_PREFIX,
    ROUNDS_EXHAUSTED_HOLD_REASON,
)
from kew.graph.driver.overlay import DriverStatus, admission_refused_hold_reason
from kew.graph.driver.tick import _FIXED_WAITING_REASONS
from kew.run_store import EpicHold
from kew.service import dashboard, needs_you
from kew.service.attention import (
    compose_node_attention,
    node_wait,
    say_wait,
    wait_link,
    wait_reason,
    wait_sentence,
)
from kew.waits import (
    HOST_RESTART_REPEATED_CONTINUATION_REASON,
    HOST_RESTART_REPEATED_REASON,
    STALL_BUCKETS,
    UNBUCKETED_HELD_COMMAND,
    fill_command,
)

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
HELD_AT = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
EPIC = "go-live-2026-09"
ITEM = "SFD-232"
#: The cap key the server writes into a review-budget hold reason, which is the
#: one thing that row's words have to keep: the operator's move is to raise it.
CAP_KEY = "[review].max_review_usd"
REVIEW_BUDGET_REASON = f"{REVIEW_BUDGET_HOLD_PREFIX}: $4.00 over — raise {CAP_KEY}"
#: The run a publication wait names in the one command anybody wrote down for
#: it: ``kew finalize <run_id>`` takes a run id, which no bucket template has.
RUN_ID = "run-9f2a4c"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gh-backed ready queue so unit tests never shell out."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])


def _hold(reason: str, item: str = ITEM) -> EpicHold:
    return EpicHold(0, EPIC, reason, HELD_AT, item)


def _node(node_id: str, status: str, **extra: Any) -> dict[str, Any]:
    node = {
        "id": node_id,
        "title": f"node {node_id}",
        "status": status,
        "status_since": "2026-10-01T09:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    node.update(extra)
    return node


def _snapshot(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": EPIC,
        "signal": "awaiting_merges",
        "counts": {},
        "frontier": [],
        "nodes": list(nodes),
        "edges": [],
    }


def _rows(
    *,
    holds: list[EpicHold] | None = None,
    nodes: tuple[dict[str, Any], ...] = (),
    ticking: bool = True,
    syncable: list[FakeRun] | None = None,
) -> list[Any]:
    """The needs-you set over one epic, as the console's ``rows`` carries it."""
    store = FakeStore(
        epic_holds=holds or [],
        epic_refs=[EPIC],
        syncable=syncable or [],
        driver_states={
            EPIC: FakeDriverState(
                epic_ref=EPIC,
                # A heartbeat from yesterday is a driver nobody is running; one
                # from `now` is a driver mid-tick.
                last_tick_started_at=(NOW if ticking else HELD_AT).isoformat(),
            )
        },
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot(*nodes))},
    )
    return needs_you.needs_you_set(store, _config(), now=NOW).entries


def _one(rows: list[Any], item: str) -> Any:
    matching = [row for row in rows if row.item_id == item]
    assert len(matching) == 1, [(r.type, r.item_id) for r in rows]
    return matching[0]


# --------------------------------------------------------------------------- #
# The rows from the screenshots
# --------------------------------------------------------------------------- #


def test_the_round_cap_row_names_its_kind_its_words_and_its_unhold() -> None:
    """``review_rounds_exhausted`` was the row with nothing readable on it.

    Unbucketed, it reached every surface as the generic HELD fallback: the raw
    reason in place of words, no label at all for ``kew epic status``'s
    dominant-cause line, and a ``primary_action`` of "approve" that cannot
    clear a row in ``epic_holds``.
    """
    row = _one(_rows(holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON)]), ITEM)

    assert row.type == "budget_hold"  # the historical wire type is unchanged
    assert row.attention_kind == "held"
    assert row.label == "the review round cap was reached"
    assert row.command == f"kew epic unhold {EPIC} {ITEM}"


def test_the_review_budget_row_is_a_budget_kind_that_keeps_its_cap_key() -> None:
    """A cap hold's move is to raise the key its reason names, so the key has
    to survive into the row's words — and no kew command raises a cap, so the
    row offers none rather than offering an unhold that would re-dispatch into
    the same cap."""
    row = _one(_rows(holds=[_hold(REVIEW_BUDGET_REASON)]), ITEM)

    assert row.attention_kind == "budget_exhausted"
    assert row.label == "held on the review budget"
    assert CAP_KEY in row.reason
    assert row.command == ""


def test_a_wait_row_carries_its_node_s_title() -> None:
    """Work titles a row by the item, not by its wait (#2786): SFD-229 read
    "the review round cap was reached" on Work and "budget caps per epic" on
    its epic card. The title is the snapshot node's, the one the card prints."""
    node = _node(ITEM, "awaiting_continuation", title="budget caps per epic")
    row = _one(_rows(nodes=(node,)), ITEM)

    assert row.type == "epic_wait"
    assert row.title == "budget caps per epic"
    assert row.sentence != row.title


def test_a_hold_row_carries_the_title_its_live_epic_gives_the_node() -> None:
    """An ``epic_holds`` row has no snapshot node of its own, so its title is
    read off the live epic's snapshot — and is "" when the snapshot does not
    hold the node, never a stand-in."""
    held = _node(ITEM, "held", title="budget caps per epic")
    titled = _one(
        _rows(holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON)], nodes=(held,)), ITEM
    )
    untitled = _one(_rows(holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON)]), ITEM)

    assert titled.type == "budget_hold"
    assert titled.title == "budget caps per epic"
    assert untitled.title == ""


def test_an_awaiting_continuation_wait_carries_the_filled_command() -> None:
    """The SFD-232 row. The epic and the node are both in the payload already;
    what was missing was anything that put them INTO the command."""
    row = _one(_rows(nodes=(_node(ITEM, "awaiting_continuation"),)), ITEM)

    assert row.type == "epic_wait"
    assert row.attention_kind == "held"
    assert row.label == "awaiting preserved-work continuation"
    assert row.command == f"kew epic continue {EPIC} {ITEM}"
    assert "<" not in row.command and ">" not in row.command
    # The console's verbs still read `primary_action`, placeholders and all.
    assert row.primary_action == "kew epic continue <name> <node>"


def test_a_repeated_host_restart_wait_carries_the_epic_retry_command() -> None:
    node = _node("2055", "failed", stuck=HOST_RESTART_REPEATED_REASON)

    row = needs_you._epic_wait_row("factory-ui", node, frozenset())

    assert row is not None
    assert row.type == "epic_wait"
    assert row.command == "kew epic retry factory-ui 2055"


def test_a_repeated_host_restart_wait_names_its_own_attention_kind() -> None:
    """Nothing holds a node the host restarted under twice; it failed. Its row
    says so in a kind of its own, which is what the console draws the
    needs-you mark from, never the reason text (#2971)."""
    node = _node("2055", "failed", stuck=HOST_RESTART_REPEATED_REASON)

    row = needs_you._epic_wait_row("factory-ui", node, frozenset())

    assert row is not None
    assert row.attention_kind == "host_restart_repeated"


def test_a_repeated_host_restart_sentence_is_the_label_without_its_code() -> None:
    """The whole reason is the bucket's machine key, so it adds nothing to the
    label; echoing it would print a raw reason code as prose (DESIGN.md). The
    code stays on the row's ``reason`` as secondary detail."""
    reason = f"#2058 — {HOST_RESTART_REPEATED_REASON}"

    sentence = wait_sentence("host_restart_repeated", reason, "kew epic retry e 2058")

    assert sentence == "the host restarted under this node twice"
    assert "failed-terminal" not in sentence
    assert "host_restart_repeated" not in sentence


def test_a_repeated_host_restart_over_preserved_work_carries_epic_continue() -> None:
    """The dirty twin (#2964): the checkout still holds the work, so the row
    offers the command that resumes it, never a fresh retry."""
    node = _node(
        "2055",
        "held",
        stuck=f"{HOST_RESTART_REPEATED_CONTINUATION_REASON}; preserved worktree",
    )

    row = needs_you._epic_wait_row("factory-ui", node, frozenset())

    assert row is not None
    assert row.type == "epic_wait"
    assert row.command == "kew epic continue factory-ui 2055"


def test_a_command_quoted_in_the_stored_reason_never_reaches_the_row() -> None:
    """The go-live journal predates #2244 and still stores the old sentence,
    so the row's words printed `kew epic resume` beside the computed `kew epic
    continue` (Tom's screenshot, 2026-10-03). Commands are computed, never read
    out of text: the quoted one and its "resume with" clause are dropped."""
    stored = (
        "awaiting_continuation — preserved worktree; "
        f"resume with `kew epic resume {EPIC} {ITEM}`"
    )
    row = _one(_rows(nodes=(_node(ITEM, "held", stuck=stored),)), ITEM)

    assert row.reason == f"{ITEM} — awaiting_continuation — preserved worktree"
    assert "`" not in row.reason and "resume" not in row.reason
    assert row.command == f"kew epic continue {EPIC} {ITEM}"


def test_a_reason_that_is_only_a_quoted_command_falls_back_to_the_label() -> None:
    stored = f"awaiting_continuation; resume with `kew epic resume {EPIC} {ITEM}`"
    row = _one(_rows(nodes=(_node(ITEM, "held", stuck=stored),)), ITEM)
    assert row.reason == f"{ITEM} — awaiting_continuation"
    assert row.command == f"kew epic continue {EPIC} {ITEM}"


# --------------------------------------------------------------------------- #
# A dropped command takes its clause with it, and is only dropped for a
# computed one (#2276)
# --------------------------------------------------------------------------- #

#: Where the driver writes a backticked ``kew`` command into a waiting reason
#: TODAY, and what each must read as once the computed command replaces the
#: quoted one. Taken from the producers themselves where they are reachable,
#: because the first pass at this lifted the backticked span ALONE and left
#: every one of these mid-sentence: "preserved dirty work; use or",
#: "fix the cause, then un-", "no image is mapped for runner claude —".
TODAYS_QUOTED_COMMANDS = (
    pytest.param(
        _FIXED_WAITING_REASONS[DriverStatus.AWAITING_CONTINUATION],
        "awaiting_continuation — preserved dirty work",
        id="two-alternatives-no-introducer",
    ),
    pytest.param(
        "awaiting_continuation — preserved worktree; continue with "
        f"`kew epic continue {EPIC} {ITEM}` or abandon with "
        f"`kew epic abandon {EPIC} {ITEM} --reason TEXT`",
        "awaiting_continuation — preserved worktree",
        id="two-alternatives-each-introduced",
    ),
    pytest.param(
        admission_refused_hold_reason("the issuer is unreachable", ITEM),
        "admission_refused: the issuer is unreachable — fix the cause",
        id="hyphenated-introducer",
    ),
    pytest.param(
        f"{CONTAINMENT_HOLD_PREFIX}: no image is mapped for runner claude — "
        f"un-hold with `kew epic unhold {ITEM}`",
        f"{CONTAINMENT_HOLD_PREFIX}: no image is mapped for runner claude",
        id="dash-introduced-remedy",
    ),
)


def _wait_line(stored: str, status: str = "held", epic_ref: str = EPIC) -> str:
    """One journaled wait's words, as the row and the popover both compose them.

    Both surfaces fill the bucket's template first and hand the result to
    ``wait_reason`` (they are held to one command by
    ``test_the_row_and_the_diamond_over_one_wait_print_one_command``), so the
    join they share is read here directly — an admission or containment reason
    only reaches it when the hold table is UNREADABLE, since a readable one
    answers that node with its own hold row instead.
    """
    wait = node_wait(status, stored)
    assert wait is not None, stored
    command = fill_command(wait.command, epic=epic_ref, item=ITEM)
    assert command, stored  # all four are reasons a kew command does clear
    return wait_reason(wait, status, stored, command)


@pytest.mark.parametrize(("stored", "expected"), TODAYS_QUOTED_COMMANDS)
def test_a_dropped_command_takes_its_whole_clause_with_it(
    stored: str, expected: str
) -> None:
    """Half a clause is not a sentence: the words that introduce the command
    and any alternative chained onto it go with it, and nothing is left
    dangling at the seam."""
    line = _wait_line(stored)

    assert line == expected
    assert "`" not in line
    assert line.split()[-1] not in {"or", "and", "use", "with", "then", "un-"}
    assert not line.endswith(("-", "—", ",", ";", ":"))


def test_a_quoted_alternative_does_not_outlive_the_command_it_hangs_off() -> None:
    """The reason the preserved-work marker writes, as the row carries it: the
    row's one command is the computed ``kew epic continue``, and "or abandon
    with `kew epic abandon ...`" is not a second one to print beside it."""
    stored = (
        "awaiting_continuation — preserved worktree; continue with "
        f"`kew epic continue {EPIC} {ITEM}` or abandon with "
        f"`kew epic abandon {EPIC} {ITEM} --reason TEXT`"
    )
    row = _one(_rows(nodes=(_node(ITEM, "held", stuck=stored),)), ITEM)

    assert row.reason == f"{ITEM} — awaiting_continuation — preserved worktree"
    assert row.command == f"kew epic continue {EPIC} {ITEM}"


def test_a_publication_wait_keeps_the_only_handle_anybody_wrote_down() -> None:
    """``awaiting_publication`` is a wait no kew command template clears, so
    the row computes none — and the ``kew finalize <run_id>`` the driver wrote
    into the reason is then the only actionable thing on the row. Dropping a
    quoted command is for replacing it, never for leaving a row with nothing."""
    stored = (
        "awaiting_publication — publication unavailable; open an external PR "
        f"and reconcile, or use `kew finalize {RUN_ID}`"
    )
    nodes = (_node(ITEM, "awaiting_publication", stuck=stored),)
    row = _one(_rows(nodes=nodes), ITEM)
    attention = _join(nodes=nodes)[ITEM]

    assert row.command == attention.command == ""
    assert f"kew finalize {RUN_ID}" in row.reason
    assert row.reason == f"{ITEM} — {stored}"
    assert attention.reason == stored


def test_a_wait_whose_template_filled_nothing_keeps_the_quoted_command() -> None:
    """The same rule one step further in: a join that named no epic serves no
    command (``test_a_join_that_names_no_epic_serves_no_command``), so there is
    nothing to replace the driver's own sentence with, and it stays."""
    stored = (
        "awaiting_continuation — preserved worktree; continue with "
        f"`kew epic continue {EPIC} {ITEM}`"
    )
    attention = _join(nodes=(_node(ITEM, "held", stuck=stored),), epic_ref=None)[ITEM]

    assert attention.command == ""
    assert attention.reason == stored


def test_a_ci_pending_hold_still_produces_no_row_at_all() -> None:
    """An operator cannot make a check finish (``kew.waits.classify_wait``), so
    the CI gate has no row to carry a kind, words or a command."""
    assert _rows(holds=[_hold(CI_PENDING_HOLD_REASON)]) == []


def test_a_stalled_epic_names_the_command_that_restarts_its_driver() -> None:
    """``primary_action`` says "resume", which is the console's verb and not a
    command: ``kew resume`` clears the fleet-stop latch and would not tick this
    epic once."""
    rows = _rows(ticking=False)

    (row,) = [r for r in rows if r.type == "stalled_epic"]
    assert row.primary_action == "resume"
    assert row.command == f"kew epic run {EPIC}"


def _merge_decision_row() -> Any:
    """The set's own row for an item whose artifact awaits a human decision."""
    run = FakeRun(
        run_id="r-1",
        issue_num=0,
        item_id=ITEM,
        status="success",
        pr_state="open",
        pr_url="https://example.invalid/pr/9",
        started_at=HELD_AT,
        finished_at=HELD_AT,
    )
    return _one(_rows(syncable=[run]), ITEM)


def test_a_work_row_reads_its_kind_from_the_projection_s_own_join() -> None:
    """One item's exception may not be filed under two kinds: the row and the
    epic DAG's diamond over it both read ``attention.exception_kind``."""
    row = _merge_decision_row()

    assert row.type == "pr_review"
    assert row.attention_kind == "merge_decision"
    # A merge decision is made on the host, and no kew command makes it.
    assert row.command == ""


def _console_dashboard_fixture() -> dict[str, Any]:
    """The committed ``/api/dashboard`` payload the browser is served in
    fixture mode (``npm run dev:fixtures``), parsed."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "frontend" / "fixtures" / "factory" / "dashboard.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    pytest.skip("the frontend fixtures are not present in this checkout")


def test_the_committed_console_fixture_serves_a_merge_row_no_command() -> None:
    """The browser's own fixture payload carries the command this module's
    tables really serve for that row, and not one chosen for a screenshot.

    A design round wanted a command block photographed on ``/work``, and the
    fixture's merge-decision row was given
    ``kew epic merge <epic> <item> --head <sha>`` to produce one. No reachable
    server state serves that: a merge-decision row is :func:`_work_row`'s, its
    command is ``SAFEST_COMMAND[safest_action]``, and ``await_human_decision``
    has no entry in that table because the merge is made on the host. So the
    Copy affordance the console drew over it, and the two frontend tests
    anchored on it, asserted a value nothing could ever produce.

    The expectation is READ OFF the server rather than written down here, so a
    table that grows a merge command one day updates this with it.
    """
    served = _merge_decision_row()
    assert served.attention_kind == "merge_decision"

    rows = _console_dashboard_fixture()["rows"]
    mergers = [row for row in rows if row.get("attention_kind") == "merge_decision"]
    # Not vacuous: that row IS the merge decision /work draws its block over.
    assert mergers, [row.get("attention_kind") for row in rows]
    for row in mergers:
        assert row.get("command", "") == served.command


def _commanded_fixture_rows() -> list[dict[str, Any]]:
    """The committed ``/api/dashboard`` rows that carry a command."""
    return [row for row in _console_dashboard_fixture()["rows"] if row.get("command")]


def test_the_committed_console_fixture_reaches_a_command_block() -> None:
    """The console's Do this block has to be reachable from committed state.

    Emptying the invented merge command (above) left the fixture with no
    command on any row, and the Copy affordance is drawn only where one is
    (``block.command !== ''`` in NeedsYouBlock.svelte). So fixture-mode
    ``/work`` and Monitor's holds column had nothing to copy and nothing to
    photograph: a design round cannot check an affordance no committed payload
    reaches. The honest fix is not a command written onto the merge row, it is
    a row whose wait kew really does have a command for — which is what the
    payload now also carries.
    """
    commanded = _commanded_fixture_rows()
    known = _cli_command_paths()

    assert commanded, "no committed /api/dashboard row carries a command"
    for row in commanded:
        command = row["command"]
        assert "<" not in command and ">" not in command, command
        assert _names_a_real_command(command, known), command


def test_every_commanded_fixture_row_is_the_row_the_server_serves() -> None:
    """And the rest of that row is the server's too, field for field.

    The row is REBUILT here, by handing :func:`_epic_wait_row` a journaled node
    carrying the row's own epic, item, stamp, URL and waiting reason, and the
    result is compared with what the fixture committed. So the kind, the words,
    the action and the command are all read off ``kew.waits`` rather than typed
    into a JSON file — the failure mode this module exists for.
    """
    for row in _commanded_fixture_rows():
        item = row["item_id"]
        node = _node(
            item,
            "held",
            status_since=row["age"],
            url=row["url"],
            # `_epic_wait_row` prefixes the item ref onto the driver's own
            # reason, so the tail of the committed line is what it was built
            # from.
            stuck=row["reason"].split(" — ", 1)[1],
            title=row.get("title", ""),
        )
        served = needs_you._epic_wait_row(row["epic_ref"], node, frozenset({item}))

        assert served is not None, row
        # The set writes the sentence once its entries are collapsed (#2539).
        say_wait(served, served.attention_kind)
        for field, value in row.items():
            assert getattr(served, field) == value, (field, item)


# --------------------------------------------------------------------------- #
# Every command is a command
# --------------------------------------------------------------------------- #


def _cli_command_paths() -> set[str]:
    """Every *runnable* command path the Typer app exposes, as an operator types it.

    Groups are left out on purpose. ``kew epic`` on its own runs nothing, so a
    set that carried it would let any sentence opening with a real group name
    pass — and ``kew epic resume <epic> <item>`` is exactly that sentence.
    """

    def walk(group: Any, prefix: str) -> list[str]:
        paths: list[str] = []
        for name, sub in sorted(getattr(group, "commands", {}).items()):
            path = f"{prefix} {name}".strip()
            # A group contributes its leaves and not itself; a command has no
            # children and is its own leaf.
            paths.extend(walk(sub, path) or [path])
        return paths

    paths = set(walk(get_command(app), ""))
    assert "epic unhold" in paths, "the app walk found no commands to check against"
    assert "epic" not in paths, "a group is not a runnable command path"
    return paths


def _names_a_real_command(command: str, known: set[str]) -> bool:
    """Whether *command* opens with ``kew`` and a runnable command path.

    *known* holds leaves only, so a prefix match means the whole subcommand
    path is there: whatever follows it is an argument. A sentence that names a
    subcommand the app does not have matches nothing, however real the group it
    opens with.
    """
    words = command.split()
    if not words or words[0] != "kew":
        return False
    rest = words[1:]
    return any(" ".join(rest[:n]) in known for n in range(1, len(rest) + 1))


@pytest.mark.parametrize(
    "template",
    [
        *(bucket.command for bucket in STALL_BUCKETS if bucket.command),
        UNBUCKETED_HELD_COMMAND,
        *needs_you.SAFEST_COMMAND.values(),
    ],
)
def test_every_command_template_names_a_subcommand_the_app_has(
    template: str,
) -> None:
    """The failure this issue is named for, caught from the vocabulary side.

    ``kew epic resume`` was printed for months because nothing compared the
    sentence against the app: the commands are ``continue``, ``retry``,
    ``unhold`` and the rest of ``@epic_app.command`` in ``cli/epic.py``. So the
    check walks the real Typer app rather than listing names a second time.
    """
    filled = fill_command(template, epic=EPIC, item=ITEM, run="r-1")

    assert filled, f"{template!r} filled to nothing over a plain epic and item"
    assert _names_a_real_command(filled, _cli_command_paths()), filled


def test_every_command_a_row_serves_is_runnable_as_printed() -> None:
    """The same check over the payload, so a row cannot acquire a command by
    some route the template table does not cover."""
    known = _cli_command_paths()
    rows = _rows(
        holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON), _hold(REVIEW_BUDGET_REASON, "7")],
        nodes=(
            _node(ITEM, "awaiting_continuation"),
            _node("2003", "awaiting_publication"),
            _node("2004", "awaiting_no_change"),
            _node("2001", "in_review", pr_url="https://example.invalid/pr/1"),
        ),
        ticking=False,
    )

    commands = [row.command for row in rows if row.command]
    assert commands, "no row carried a command at all"
    for command in commands:
        assert "<" not in command and ">" not in command, command
        assert _names_a_real_command(command, known), command


@pytest.mark.parametrize(
    "command",
    [
        # The sentence #2244 is named for. `epic` is a real group and `resume`
        # is not one of its commands, so a guard that matched the group alone
        # passed the very string it exists to reject.
        f"kew epic resume {EPIC} {ITEM}",
        # The same shape in any future template: a real group, an invented
        # subcommand under it.
        "kew report resync-everything",
        "kew epic unhold-everything",
        # A group on its own runs nothing.
        "kew epic",
        # And the frame still has to be kew's.
        f"kew-ng epic unhold {EPIC} {ITEM}",
        "epic unhold",
        "",
    ],
)
def test_the_guard_rejects_a_sentence_that_is_not_a_command(command: str) -> None:
    """The guard above is the only thing standing between a template and the
    console, so it gets held to the failures it is meant to catch."""
    assert not _names_a_real_command(command, _cli_command_paths())


@pytest.mark.parametrize(
    "command",
    [
        f"kew epic unhold {EPIC} {ITEM}",
        f"kew epic continue {EPIC} {ITEM}",
        f"kew epic run {EPIC}",
        "kew report sync",
        "kew finalize r-1",
    ],
)
def test_the_guard_accepts_the_commands_the_table_really_names(command: str) -> None:
    assert _names_a_real_command(command, _cli_command_paths())


# --------------------------------------------------------------------------- #
# fill_command: a command is served only when it is runnable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("template", "values"),
    [
        # Nothing to fill.
        ("", {"epic": EPIC, "item": ITEM}),
        # A token with no value: `kew epic unhold go-live` is not runnable.
        ("kew epic unhold {epic} {item}", {"epic": EPIC, "item": ""}),
        ("kew epic run {epic}", {"epic": ""}),
        # A token nobody filled survives, which is the `<name> <node>` bug.
        ("kew epic unhold {epic} {item}", {"epic": EPIC}),
        # An argument that is not a plain argument. An epic ref and a node id
        # are tracker text, and the field exists to be pasted into a shell.
        ("kew epic run {epic}", {"epic": "go live"}),
        ("kew epic run {epic}", {"epic": "go-live; rm -rf /"}),
        ("kew epic unhold {epic} {item}", {"epic": EPIC, "item": "<node>"}),
        # An argument slot that fills with an option stops being an argument:
        # `kew epic run --all` runs every epic, not the one the row names.
        ("kew epic run {epic}", {"epic": "--all"}),
        ("kew epic unhold {epic} {item}", {"epic": EPIC, "item": "-f"}),
    ],
)
def test_a_command_that_would_not_run_is_served_as_nothing(
    template: str, values: dict[str, str]
) -> None:
    assert fill_command(template, **values) == ""


def test_a_filled_command_keeps_the_ids_a_tracker_really_uses() -> None:
    assert (
        fill_command("kew epic unhold {epic} {item}", epic="epic:go-live", item="SFD-2")
        == "kew epic unhold epic:go-live SFD-2"
    )


def test_a_dash_inside_a_ref_is_still_a_ref() -> None:
    """Only a LEADING dash makes an argument an option, and real epic refs
    carry dashes in the middle."""
    assert (
        fill_command("kew epic run {epic}", epic="go-live-2026-09")
        == "kew epic run go-live-2026-09"
    )


# --------------------------------------------------------------------------- #
# The epic DAG's node join carries the same command (#2250)
# --------------------------------------------------------------------------- #
#
# The popover beside a selected DAG node prints a command under a `$` prompt
# and a Copy button, and its first round composed that command in TypeScript by
# taking the leading words of ``attention.action``. But ``action`` is
# ``kew.waits``' PROSE about the move, so every command it could yield was
# argument-less: ``kew epic unhold`` exits 2 on "Missing argument 'NAME'",
# ``kew epic retry the`` exits 2 on "Missing argument 'NODE'", and ``kew epic
# continue <name> <node>`` exits 2 on the placeholder. A Copy button over a
# line that exits 2 is worse than no button.
#
# So the node join carries ``command`` as well, filled through the same guard
# as the rows above, and the frontend prints it verbatim.


def _join(
    *,
    holds: list[EpicHold] | None = None,
    nodes: tuple[dict[str, Any], ...] = (),
    epic_ref: str | None = EPIC,
) -> dict[str, Any]:
    """The needs-you join over one epic's nodes, as the DAG payload carries it."""
    store = FakeStore(
        epic_holds=holds or [],
        epic_refs=[EPIC],
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot(*nodes))},
    )
    ids = [str(n["id"]) for n in nodes] + [h.item_id for h in holds or []]
    return compose_node_attention(
        store,
        _config(),
        ids,
        nodes=list(nodes),
        epic_ref=epic_ref,
        signal="awaiting_merges",
        now=NOW,
    ).by_node


def test_a_node_s_attention_carries_a_filled_command_not_its_own_prose() -> None:
    """The round cap, as the DAG popover reads it."""
    attention = _join(holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON)])[ITEM]

    assert attention.command == f"kew epic unhold {EPIC} {ITEM}"
    # The prose is unchanged, and is still not a command: its leading words
    # are the whole of what the first round of the popover offered.
    assert attention.action == "kew epic unhold, which also restarts the review cycle"
    assert attention.action.split(",")[0] == "kew epic unhold"


def test_an_awaiting_continuation_node_fills_the_placeholders_too() -> None:
    """``kew epic continue <name> <node>`` is the hint the console shipped with
    its placeholders still in it (#2244) — the same sentence the DAG popover
    was scraping a year of screenshots later."""
    attention = _join(nodes=(_node(ITEM, "awaiting_continuation"),))[ITEM]

    assert attention.command == f"kew epic continue {EPIC} {ITEM}"
    assert "<" not in attention.command and ">" not in attention.command
    assert attention.action == "kew epic continue <name> <node>"


def test_a_wait_no_kew_command_clears_offers_the_popover_nothing() -> None:
    """No command is the honest answer where no command exists: raising a cap
    is a config edit, and a merge decision happens on the host."""
    held = _join(holds=[_hold(REVIEW_BUDGET_REASON)])[ITEM]
    merge = _join(nodes=(_node("2001", "in_review"),))["2001"]

    assert held.command == ""
    assert merge.command == ""


def test_a_join_that_names_no_epic_serves_no_command() -> None:
    """Half a command is the defect, so an unnamed epic serves none at all."""
    attention = _join(nodes=(_node(ITEM, "awaiting_continuation"),), epic_ref=None)[
        ITEM
    ]

    assert attention.command == ""
    # And the wait itself still reaches the diamond: the command is the only
    # thing the missing epic ref costs.
    assert attention.kind == "held"


def test_every_command_the_node_join_serves_is_runnable_as_printed() -> None:
    """The rows' own check (above), over the other reading of the same set: a
    popover cannot acquire a command by a route the template table misses."""
    known = _cli_command_paths()
    by_node = _join(
        holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON), _hold(REVIEW_BUDGET_REASON, "7")],
        nodes=(
            _node("2002", "awaiting_continuation"),
            _node("2003", "awaiting_publication"),
            _node("2004", "awaiting_no_change"),
            _node("2001", "in_review", pr_url="https://example.invalid/pr/1"),
        ),
    )

    commands = [a.command for a in by_node.values() if a.command]
    assert commands, "no node carried a command at all"
    for command in commands:
        assert "<" not in command and ">" not in command, command
        assert _names_a_real_command(command, known), command


def test_the_row_and_the_diamond_over_one_wait_print_one_command() -> None:
    """Two readings of one wait, which is the disagreement #2207 exists to
    remove: the judgment row and the DAG popover for SFD-232 are composed by
    different modules off the same bucket, so they must agree word for word."""
    nodes = (_node(ITEM, "awaiting_continuation"),)
    row = _one(_rows(nodes=nodes), ITEM)
    attention = _join(nodes=nodes)[ITEM]

    assert row.command == attention.command == f"kew epic continue {EPIC} {ITEM}"
    assert row.attention_kind == attention.kind


# --------------------------------------------------------------------------- #
# One sentence per wait (#2539)
# --------------------------------------------------------------------------- #


def test_the_row_and_the_diamond_over_one_merge_say_one_sentence() -> None:
    """One merge decision read four ways in the console — "kew opens the pull
    request…", "PR open for #2055 — awaiting review", "round 2 READY" — because
    every surface wrote its own. The server writes it once, on both DTOs."""
    pr = "https://example.invalid/pr/41"
    nodes = (_node(ITEM, "in_review", pr_url=pr),)
    row = _one(_rows(nodes=nodes), ITEM)
    attention = _join(nodes=nodes)[ITEM]

    sentence = "PR #41 is open — the merge decision is yours"
    assert row.sentence == attention.sentence == sentence
    assert row.link is not None and attention.link is not None
    assert row.link.label == attention.link.label == "Open PR #41 to merge"
    assert row.link.href == attention.link.href == pr


def test_a_projection_merge_decision_says_the_same_sentence() -> None:
    """The work projection's own merge row, not only the journaled wait."""
    row = _merge_decision_row()

    assert row.sentence == "PR #9 is open — the merge decision is yours"
    assert row.link is not None and row.link.label == "Open PR #9 to merge"
    # Its move is the link; no kew command makes a merge.
    assert row.command == ""


def test_the_row_and_the_diamond_over_one_hold_say_one_sentence() -> None:
    """A hold's sentence is the bucket's label and what the reason adds, with
    the item reference left to the column that prints it."""
    holds = [_hold(ROUNDS_EXHAUSTED_HOLD_REASON)]
    row = _one(_rows(holds=holds), ITEM)
    attention = _join(holds=holds)[ITEM]

    assert row.sentence == attention.sentence
    assert row.sentence.startswith("the review round cap was reached")
    assert ITEM not in row.sentence
    assert row.link is None and attention.link is None


def test_a_wait_with_no_command_keeps_the_cap_to_raise() -> None:
    """A cap is raised by a config edit, so no command carries the key — the
    sentence is the only place left to say it."""
    attention = _join(holds=[_hold(REVIEW_BUDGET_REASON)])[ITEM]

    assert attention.command == ""
    assert CAP_KEY in attention.sentence


def test_every_row_the_set_serves_carries_a_sentence() -> None:
    """No surface composes one, so an empty sentence would print nothing."""
    rows = _rows(
        holds=[_hold(ROUNDS_EXHAUSTED_HOLD_REASON)],
        nodes=(_node("2002", "awaiting_continuation"),),
        ticking=False,
    )

    assert rows
    for row in rows:
        assert row.sentence.strip(), row


def test_an_issue_url_is_never_called_a_pull_request() -> None:
    """A merge wait whose node has no PR serves its ISSUE url; "PR #2055 is
    open" over that would name a pull request that does not exist."""
    issue = "https://github.com/o/r/issues/2055"
    pr = "https://github.com/o/r/pull/2059"

    assert wait_sentence("merge_ready", "in_review awaiting merge", "", issue) == (
        "in_review awaiting merge"
    )
    assert wait_link("merge_ready", issue) is None
    assert wait_sentence("merge_ready", "in_review", "", pr).startswith("PR #2059")
    # And only a merge is a link at all.
    assert wait_link("held", pr) is None
