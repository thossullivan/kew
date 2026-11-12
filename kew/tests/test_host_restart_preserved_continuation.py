"""A second host death of preserved work offers continue, not retry (#2960).

With ``[publication] publish = "checkout"``, a continuation that keeps dirty
bytes through its node's second host death is left terminal and incomplete.
``kew epic retry`` would record a marker and nothing else: the next tick reads
the checkout as pre-existing work that two killed runs share, and holds the
node again. ``kew epic continue`` is the command that resumes those bytes, so
it is the one the attention row offers, and retry refuses the node.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.driver_fakes import (
    EPIC,
    FakeFactsReader,
    FakeSource,
    _config,
    _facts,
    _node,
)

from kew.graph.build import build_graph
from kew.graph.driver.attention import AttentionKind, attention_events
from kew.graph.driver.tick import summarize
from kew.run_store import RunRecord
from kew.service import needs_you, verbs
from kew.waits import (
    CONTINUATION_AMBIGUOUS_REASON,
    HOST_RESTART_REPEATED,
    HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
    HOST_RESTART_REPEATED_PRESERVED_REASON,
    bucket_for,
    fill_command,
)

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

ITEM = "1010"
BRANCH = "agent/issue-1010"
STARTED = datetime.now(UTC) - timedelta(hours=1)


def _killed(
    store: Any, run_id: str, category: str, *, minutes: int, **owner: Any
) -> None:
    """A dispatch run the host died under, reaped with *category*."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(ITEM),
            item_id=ITEM,
            branch=BRANCH,
            status="killed",
            exit_code=137,
            error_category=category,
            started_at=STARTED + timedelta(minutes=minutes),
            finished_at=STARTED + timedelta(minutes=minutes + 1),
            **owner,
        )
    )


def _preserved_through_second_host_death(
    store: Any,
    checkout: Path,
    *,
    original: str = "original",
    minutes: int = 0,
    epic: str = EPIC,
    owner: dict[str, Any] | None = None,
) -> Any:
    """The node's checkout continuation, terminal and incomplete.

    The original run died with the host and kept dirty bytes; its continuation
    claimed them and died with the host again.
    """
    checkout.mkdir(parents=True, exist_ok=True)
    checkout.joinpath("wip.txt").write_text("preserve me\n", encoding="utf-8")
    args = (epic, ITEM, original, BRANCH, checkout)
    continuation = f"{original}-continuation"
    _killed(
        store, original, "orphaned_pending_preserved", minutes=minutes, **(owner or {})
    )
    store.record_continuation(
        *args, checkout_generation=fixture_checkout_generation(checkout)
    )
    assert (
        store.claim_continuation_under_cap(
            *args,
            continuation,
            0,
            claim_evidence=fixture_claim_evidence(store, *args, continuation),
        )
        == "claimed"
    )
    _killed(
        store,
        continuation,
        HOST_RESTART_REPEATED,
        minutes=minutes + 10,
        **(owner or {}),
    )
    assert store.finalize_continuation(*args, 1, continuation)
    record = store.get_continuation(*args)
    assert record.state == "terminal"
    assert not store.continuation_is_complete(record)
    return record


def _ambiguous_preserved_work(store: Any, tmp_path: Path) -> None:
    """Two recoverable terminal continuations of one node, from different
    original runs: the continue selector refuses the history as ambiguous."""
    _preserved_through_second_host_death(store, tmp_path / "issue-1010")
    _preserved_through_second_host_death(
        store, tmp_path / "issue-1010-b", original="original-b", minutes=20
    )


def _checkout_config() -> Any:
    config = _config()
    config.publication.publish = "checkout"
    return config


def _summary(store: Any) -> Any:
    deps = FakeSource(nodes=[_node(ITEM)], edges=[])
    return summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_checkout_config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=datetime.now(UTC),
    )


def test_the_attention_row_offers_continue_for_preserved_work(
    store: Any, tmp_path: Path
) -> None:
    _preserved_through_second_host_death(store, tmp_path / "issue-1010")

    events = [e for e in attention_events(EPIC, _summary(store)) if e.item == ITEM]

    assert [(e.kind, e.reason) for e in events] == [
        (AttentionKind.HELD, HOST_RESTART_REPEATED_PRESERVED_REASON)
    ]
    bucket = bucket_for(events[0].reason)
    assert bucket is not None
    assert fill_command(bucket.command, epic=EPIC, item=ITEM) == (
        f"kew epic continue {EPIC} {ITEM}"
    )


def test_the_needs_you_row_carries_the_continue_command() -> None:
    node = {
        "id": "2055",
        "title": "node 2055",
        "status": "failed",
        "status_since": "2026-10-01T09:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": HOST_RESTART_REPEATED_PRESERVED_REASON,
    }

    row = needs_you._epic_wait_row("factory-ui", node, frozenset())

    assert row is not None
    assert row.command == "kew epic continue factory-ui 2055"


def test_retry_refuses_preserved_work_and_names_the_fix(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "operator")
    _preserved_through_second_host_death(store, tmp_path / "issue-1010")

    with pytest.raises(ValueError) as refused:
        verbs.epic_retry(store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config())

    message = str(refused.value)
    # The epic ref keeps its ``/``: the printed command is the one that runs.
    assert f"`kew epic continue {EPIC} {ITEM}`" in message
    assert f"`kew epic abandon {EPIC} {ITEM} --reason TEXT`" in message
    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="epic_node_retry") == []

    printed = message.split("`")[1]
    _kew, _epic, verb, epic_ref, item_id = shlex.split(printed)
    assert verb == "continue"
    claimed = verbs.epic_resume(
        store, epic_ref=epic_ref, item_id=item_id, config=_checkout_config()
    )
    assert claimed.state == "continuing"
    assert claimed.original_run_id == "original"


def test_retry_names_no_command_for_an_epic_ref_a_shell_would_split(
    store: Any, tmp_path: Path
) -> None:
    """A ref that is not a plain argument gets no command, not a rewritten one."""
    epic = "go live"
    _preserved_through_second_host_death(store, tmp_path / "issue-1010", epic=epic)

    with pytest.raises(ValueError) as refused:
        verbs.epic_retry(store, epic_ref=epic, item_id=ITEM, config=_checkout_config())

    message = str(refused.value)
    assert "preserved work that retry would not resume" in message
    assert "kew epic" not in message
    assert store.list_epic_retries(epic) == []


def test_ambiguous_preserved_work_offers_no_command(store: Any, tmp_path: Path) -> None:
    _ambiguous_preserved_work(store, tmp_path)

    events = [e for e in attention_events(EPIC, _summary(store)) if e.item == ITEM]

    assert [(e.kind, e.reason) for e in events] == [
        (AttentionKind.HELD, HOST_RESTART_REPEATED_AMBIGUOUS_REASON)
    ]
    bucket = bucket_for(events[0].reason)
    assert bucket is not None
    assert fill_command(bucket.command, epic=EPIC, item=ITEM) == ""
    with pytest.raises(verbs.ContinuationSelectionDenied):
        verbs.select_awaiting_continuation(store, EPIC, ITEM)


def test_ambiguous_preserved_work_needs_you_row_has_no_command() -> None:
    node = {
        "id": "2055",
        "title": "node 2055",
        "status": "failed",
        "status_since": "2026-10-01T09:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
    }

    row = needs_you._epic_wait_row("factory-ui", node, frozenset())

    assert row is not None
    assert row.command == ""


def test_retry_refuses_ambiguous_preserved_work_and_names_the_rows(
    store: Any, tmp_path: Path
) -> None:
    _ambiguous_preserved_work(store, tmp_path)

    with pytest.raises(ValueError) as refused:
        verbs.epic_retry(store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config())

    message = str(refused.value)
    assert "unambiguous" in message
    assert "original" in message and "original-b" in message
    assert "kew epic continue" not in message
    assert "kew epic abandon" not in message
    assert "kew epic retry" not in message
    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="epic_node_retry") == []


def test_continue_resumes_the_preserved_checkout(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "operator")
    checkout = tmp_path / "issue-1010"
    record = _preserved_through_second_host_death(store, checkout)

    claimed = verbs.epic_resume(
        store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config()
    )

    assert claimed.state == "continuing"
    assert claimed.generation == record.generation + 1
    assert claimed.worktree_identity == record.worktree_identity
    assert claimed.original_run_id == "original"
    assert claimed.continuation_run_id not in (None, "original-continuation")
    assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == ("preserve me\n")
    assert claimed.branch == record.branch


def test_the_stuck_line_names_continue_for_preserved_work() -> None:
    from kew.cli.epic import _stuck_restart
    from kew.graph.driver.result import StuckNode

    node = StuckNode(ITEM, HOST_RESTART_REPEATED_PRESERVED_REASON, ())

    assert _stuck_restart(node, "go-live") == f" — `kew epic continue go-live {ITEM}`"


def test_the_stuck_line_names_nothing_for_ambiguous_preserved_work() -> None:
    from kew.cli.epic import _stuck_restart
    from kew.graph.driver.result import StuckNode

    node = StuckNode(ITEM, HOST_RESTART_REPEATED_AMBIGUOUS_REASON, ())

    assert _stuck_restart(node, "go-live") == ""


def _mixed_history(store: Any, tmp_path: Path) -> None:
    """A recoverable terminal continuation beside an awaiting one from another
    original run: the node is not FAILED, and the selector still refuses it."""
    _preserved_through_second_host_death(store, tmp_path / "issue-1010")
    awaiting = tmp_path / "issue-1010-b"
    awaiting.mkdir()
    _killed(store, "original-b", "orphaned_pending_preserved", minutes=30)
    store.record_continuation(
        EPIC,
        ITEM,
        "original-b",
        BRANCH,
        awaiting,
        checkout_generation=fixture_checkout_generation(awaiting),
    )
    with pytest.raises(verbs.ContinuationSelectionDenied):
        verbs.select_awaiting_continuation(store, EPIC, ITEM)


def test_a_mixed_history_offers_no_command_on_the_dashboard(
    store: Any, tmp_path: Path
) -> None:
    """The project has no repo, so the dashboard's ready queue makes no gh call."""
    _mixed_history(store, tmp_path)

    _fx._advance(
        deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        store=store,
        sink=_fx.FakeSink(),
        lock_path=tmp_path / "epic.lock",
        now=datetime.now(UTC),
        config=_checkout_config(),
    )

    events = [e for e in attention_events(EPIC, _summary(store)) if e.item == ITEM]
    assert [(e.kind, e.reason) for e in events] == [
        (AttentionKind.HELD, CONTINUATION_AMBIGUOUS_REASON)
    ]
    rows = needs_you.needs_you_set(store, _checkout_config(), now=datetime.now(UTC))
    [row] = [r for r in rows.entries if r.item_id == ITEM]
    assert row.command == ""
    assert "kew epic continue" not in json.dumps(row.model_dump(mode="json"))


def test_retry_refuses_a_mixed_history_and_names_the_rows(
    store: Any, tmp_path: Path
) -> None:
    _mixed_history(store, tmp_path)

    with pytest.raises(ValueError) as refused:
        verbs.epic_retry(store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config())

    message = str(refused.value)
    assert "unambiguous" in message
    assert "original" in message and "original-b" in message
    assert "kew epic" not in message
    assert store.list_epic_retries(EPIC) == []


@pytest.fixture
def _pinned_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin the machine id: the host running the tests may have none (#2812)."""
    from kew import process_groups

    machine_id = tmp_path / "pinned-machine-id"
    machine_id.write_text("c" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (machine_id,))
    process_groups._this_machine.cache_clear()
    yield
    process_groups._this_machine.cache_clear()


def _recycled_owner_from_an_earlier_boot() -> dict[str, Any]:
    """An owner the host died under, whose pid a later boot handed to a live
    stranger (this test's own process group)."""
    from kew.process_groups import identity_at_admission, machine_boot

    current = machine_boot()
    assert current is not None, "Linux and macOS both say their boot id"
    machine, _, boot = current.rpartition("/")
    stranger = os.getpgrp()
    return {
        "pgid": stranger,
        "owner_pid": stranger,
        "attempt_pgid": stranger,
        **identity_at_admission(stranger, require_token=True),
        "owner_boot_id": f"{machine}/{'0' * 32 if boot != '0' * 32 else '1' * 32}",
    }


def _git_checkout(checkout: Path) -> None:
    """A standalone clone on the node's branch, holding uncommitted work."""
    checkout.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", BRANCH),
        ("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q")
        + ("--allow-empty", "-m", "base"),
    ):
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)


def _cli_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[dict[str, Any]]]:
    """A checkout-mode project whose node checkout holds uncommitted work, and
    the agent launches ``kew epic continue`` would make."""
    from kew import cli, subprocess_utils
    from kew.worktree import write_checkout_generation

    monkeypatch.setenv("KEW_ACTOR", "operator")
    monkeypatch.chdir(tmp_path)
    tmp_path.joinpath("kew.toml").write_text(
        f'''[project]
repo = "o/r"

[logging]
dir = "{tmp_path / "logs"}"

[branches]
worktree_base = "{tmp_path / "worktrees"}"

[publication]
publish = "checkout"
''',
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir()
    checkout = tmp_path / "worktrees" / "issue-1010"
    _git_checkout(checkout)
    write_checkout_generation(checkout)
    subprocess_utils._register_optional_tool("git")
    launched: list[dict[str, Any]] = []

    def _agent(**kwargs: Any) -> Any:
        launched.append(kwargs)
        return SimpleNamespace(exit_code=0, launched=True)

    # Process and forge boundaries only: tool presence, the GitHub issue read
    # and the agent launch.
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    monkeypatch.setattr(cli, "runner_preflight_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli,
        "select_next_issue",
        lambda **_kwargs: {"number": 1010, "title": "t", "body": "", "labels": []},
    )
    monkeypatch.setattr(cli, "execute_dispatch", _agent)

    return checkout, launched


@pytest.mark.usefixtures("_pinned_machine")
def test_the_printed_continue_resumes_past_a_recycled_owner_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from kew import cli
    from kew.run_store import SqliteRunStore

    checkout, launched = _cli_project(tmp_path, monkeypatch)

    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        record = _preserved_through_second_host_death(
            store, checkout, owner=_recycled_owner_from_an_earlier_boot()
        )
        [event] = attention_events(EPIC, _summary(store))
        bucket = bucket_for(event.reason)
        assert bucket is not None
        printed = fill_command(bucket.command, epic=EPIC, item=ITEM)

        _kew, _epic, *argv = shlex.split(printed)
        result = CliRunner().invoke(cli.epic_app, argv)

        assert result.exit_code == 0, result.output
        [launch] = launched
        assert (launch["worktree_dir"], launch["branch"]) == (checkout, BRANCH)
        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )
        [resumed] = [
            r for r in store.list_continuations(EPIC) if r.original_run_id == "original"
        ]
        assert resumed.generation == record.generation + 1


@pytest.mark.usefixtures("_pinned_machine")
def test_abandon_accepts_a_host_death_past_recycled_owner_pids(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry refusal names abandon too: it takes the same boot evidence."""
    from kew.worktree import checkout_claim_guard

    monkeypatch.setenv("KEW_ACTOR", "operator")
    _preserved_through_second_host_death(
        store, tmp_path / "issue-1010", owner=_recycled_owner_from_an_earlier_boot()
    )

    verbs.epic_abandon(
        store,
        epic_ref=EPIC,
        item_id=ITEM,
        reason="the host keeps dying",
        config=_checkout_config(),
        checkout_guard=checkout_claim_guard,
    )

    [record] = store.list_continuations(EPIC)
    assert store.continuation_was_abandoned(record)


def _awaiting(store: Any, checkout: Path, original: str, *, minutes: int) -> None:
    """A run that died keeping dirty bytes, preserved as awaiting continuation."""
    checkout.mkdir(parents=True, exist_ok=True)
    _killed(store, original, "orphaned_pending_preserved", minutes=minutes)
    store.record_continuation(
        EPIC,
        ITEM,
        original,
        BRANCH,
        checkout,
        checkout_generation=fixture_checkout_generation(checkout),
    )


def _continuing(store: Any, checkout: Path, original: str, *, minutes: int) -> None:
    """The same, with its continuation claimed by another caller."""
    _awaiting(store, checkout, original, minutes=minutes)
    args = (EPIC, ITEM, original, BRANCH, checkout)
    continuation = f"{original}-continuation"
    assert (
        store.claim_continuation_under_cap(
            *args,
            continuation,
            0,
            claim_evidence=fixture_claim_evidence(store, *args, continuation),
        )
        == "claimed"
    )


@pytest.mark.parametrize(
    ("owned", "awaiting"),
    [("original-a", "original-z"), ("original-z", "original-a")],
    ids=["owned-row-first", "owned-row-last"],
)
def test_owned_plus_awaiting_history_has_no_command_regardless_of_record_order(
    store: Any, tmp_path: Path, owned: str, awaiting: str
) -> None:
    """Continue refuses while another caller owns a continuation, so no surface
    may offer it, whichever row the store lists last."""
    _continuing(store, tmp_path / owned, owned, minutes=0)
    _awaiting(store, tmp_path / awaiting, awaiting, minutes=20)
    with pytest.raises(verbs.ContinuationSelectionDenied):
        verbs.select_awaiting_continuation(store, EPIC, ITEM)

    events = [e for e in attention_events(EPIC, _summary(store)) if e.item == ITEM]
    assert events, "the preserved work still asks for a person"
    for event in events:
        assert "kew epic" not in event.reason
        bucket = bucket_for(event.reason)
        command = bucket.command if bucket is not None else ""
        assert fill_command(command, epic=EPIC, item=ITEM) == ""

    with pytest.raises(ValueError) as refused:
        verbs.epic_retry(store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config())
    assert "in progress" in str(refused.value)
    assert "kew epic" not in str(refused.value)
    assert store.list_epic_retries(EPIC) == []


def test_retry_loses_race_to_terminal_preserved_continuation_without_writes(
    store: Any, tmp_path: Path
) -> None:
    """Driver recovery preserves the node's work between retry's first read and
    its write: the write re-reads under its lock and records nothing."""
    from unittest.mock import MagicMock, patch

    driver = store.open_sibling()
    attested = store.mark_epic_retry_item_attested

    def _recovery_lands_first(*args: Any, **kwargs: Any) -> None:
        _preserved_through_second_host_death(driver, tmp_path / "issue-1010")
        attested(*args, **kwargs)

    store.mark_epic_retry_item_attested = _recovery_lands_first
    try:
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(ValueError, match="preserved work"):
                verbs.epic_retry(
                    store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config()
                )
    finally:
        driver.close()

    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="epic_node_retry") == []
    gh.assert_not_called()


_CONTAINER_ID = "a" * 64
_CONTAINER = {
    "container_name": "kew-issue-1010",
    "container_id": _CONTAINER_ID,
    "container_runtime": "docker",
}


def _docker_inspect_answers(monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    """The container daemon is a process boundary: answer ``docker inspect``
    for the recorded container, which outlived the boot its owner ran under."""
    from kew import stop_recovery

    def _run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[1:3] == ["inspect", "--format"], argv
        state = "true" if running else "false"
        out = f"{_CONTAINER_ID} /{_CONTAINER['container_name']} {state}\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(
        stop_recovery, "shutil", SimpleNamespace(which=lambda tool: f"/bin/{tool}")
    )
    monkeypatch.setattr(
        stop_recovery,
        "subprocess",
        SimpleNamespace(run=_run, SubprocessError=subprocess.SubprocessError),
    )


@pytest.mark.usefixtures("_pinned_machine")
@pytest.mark.parametrize(
    ("running", "proceeds"), [(True, False), (False, True)], ids=["running", "stopped"]
)
def test_old_boot_running_container_blocks_continue_and_abandon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: bool, proceeds: bool
) -> None:
    """An earlier boot proves the owner's processes dead, not its container:
    the daemon outlives the boot, and a running one may still write the
    checkout."""
    from typer.testing import CliRunner

    from kew import cli
    from kew.run_store import SqliteRunStore
    from kew.worktree import checkout_claim_guard

    checkout, launched = _cli_project(tmp_path, monkeypatch)
    _docker_inspect_answers(monkeypatch, running)

    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        _preserved_through_second_host_death(
            store,
            checkout,
            owner={**_recycled_owner_from_an_earlier_boot(), **_CONTAINER},
        )

        result = CliRunner().invoke(cli.epic_app, ["continue", EPIC, ITEM])
        assert (result.exit_code == 0) is proceeds, result.output
        assert bool(launched) is proceeds
        if proceeds:
            return
        assert "live or unproven owner" in result.output

        with pytest.raises(verbs.EpicAbandonDenied, match="wait until the owner"):
            verbs.epic_abandon(
                store,
                epic_ref=EPIC,
                item_id=ITEM,
                reason="the host keeps dying",
                config=_checkout_config(),
                checkout_guard=checkout_claim_guard,
            )
        [record] = store.list_continuations(EPIC)
        assert record.state == "terminal"
        assert not store.continuation_was_abandoned(record)
        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )


@pytest.mark.usefixtures("_pinned_machine")
@pytest.mark.parametrize(
    ("running", "proceeds"), [(True, False), (False, True)], ids=["running", "stopped"]
)
def test_a_live_later_continuation_container_blocks_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: bool, proceeds: bool
) -> None:
    """The original owner died with the host, but the continuation that claimed
    the checkout after it still has a container that may be writing there:
    continue must not launch a second writer into the same checkout."""
    from typer.testing import CliRunner

    from kew import cli
    from kew.run_store import SqliteRunStore

    checkout, launched = _cli_project(tmp_path, monkeypatch)
    _docker_inspect_answers(monkeypatch, running)

    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        _preserved_through_second_host_death(
            store, checkout, owner=_recycled_owner_from_an_earlier_boot()
        )
        # Only the later generation ran in a container.
        later = store.get("original-continuation")
        for field, value in _CONTAINER.items():
            setattr(later, field, value)
        store.save(later, update_ownership=True)

        result = CliRunner().invoke(cli.epic_app, ["continue", EPIC, ITEM])

        assert (result.exit_code == 0) is proceeds, result.output
        assert bool(launched) is proceeds
        if proceeds:
            return
        assert "live or unproven owner" in result.output
        [record] = store.list_continuations(EPIC)
        assert record.state == "terminal"
        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )


def _journal_stalled_retry_reason(store: Any, reason: str | None = None) -> None:
    """A stalled tick journaled before the preserved reason existed: the node
    reads plain repeated host death, whose command is retry — or *reason*."""
    from kew.waits import HOST_RESTART_REPEATED_REASON

    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=datetime.now(UTC).isoformat(),
        snapshot={
            "schema": 1,
            "epic_ref": EPIC,
            "signal": "stalled",
            "counts": {"failed": 1},
            "frontier": [],
            "nodes": [
                {
                    "id": ITEM,
                    "title": "node 1010",
                    "status": "failed",
                    "status_since": STARTED.isoformat(),
                    "url": None,
                    "pr_url": None,
                    "external": False,
                    "stuck": reason or HOST_RESTART_REPEATED_REASON,
                }
            ],
            "edges": [],
        },
        actions={"schema": 1},
        snapshot_hash="pre-2960",
    )


def test_a_pre_change_journal_reconciles_to_continue_on_every_surface(
    store: Any, tmp_path: Path
) -> None:
    """A stalled epic may never tick again, so the journal it left is read
    against the continuation history as it stands now."""
    from kew.graph.driver.attention_journal import (
        journaled_attention_events,
        read_journaled_observations,
    )
    from kew.service.epics import get_epic_detail

    _preserved_through_second_host_death(store, tmp_path / "issue-1010")
    _journal_stalled_retry_reason(store)
    expected = f"kew epic continue {EPIC} {ITEM}"

    # Drive's notifications read the journal.
    observations = read_journaled_observations(store.latest_epic_tick(EPIC))
    assert observations is not None
    [event] = journaled_attention_events(EPIC, observations, store=store)
    assert event.reason == HOST_RESTART_REPEATED_PRESERVED_REASON
    bucket = bucket_for(event.reason)
    assert bucket is not None
    assert fill_command(bucket.command, epic=EPIC, item=ITEM) == expected

    # The console's needs-you row and the epic page's node attention.
    rows = needs_you.needs_you_set(store, _checkout_config(), now=datetime.now(UTC))
    [row] = [r for r in rows.entries if r.item_id == ITEM]
    assert row.command == expected
    detail = get_epic_detail(store, EPIC, config=_checkout_config())
    assert detail is not None
    [node] = [n for n in detail.snapshot.nodes if n.id == ITEM]
    assert node.attention is not None
    assert node.attention.command == expected
    assert node.recovery_command == expected
    assert "kew epic retry" not in json.dumps(detail.model_dump(mode="json"))


def _take_up(store: Any, how: str) -> None:
    """Take the preserved work up the way an operator does, without a tick:
    abandon it (which re-admits the node), or continue it (which owns it)."""
    from kew.worktree import checkout_claim_guard

    if how == "abandon":
        verbs.epic_abandon(
            store,
            epic_ref=EPIC,
            item_id=ITEM,
            reason="the host keeps dying",
            config=_checkout_config(),
            checkout_guard=checkout_claim_guard,
        )
    else:
        claimed = verbs.epic_resume(
            store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config()
        )
        assert claimed.state == "continuing"


@pytest.mark.usefixtures("_pinned_machine")
@pytest.mark.parametrize("how", ["abandon", "continue"])
def test_taken_up_preserved_work_offers_no_command_without_a_tick(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The stalled snapshot keeps the preserved reason the driver wrote, and no
    tick rewrites it after the operator acts: every surface reads it against
    the history as it stands now, and none offers a command — continue
    refuses the abandoned or owned record, and retry refuses owned work."""
    from kew.graph.driver.attention_journal import (
        journaled_attention_events,
        read_journaled_observations,
    )
    from kew.service.epics import fetch_epic_frames, get_epic_detail

    monkeypatch.setenv("KEW_ACTOR", "operator")
    _preserved_through_second_host_death(
        store, tmp_path / "issue-1010", owner=_recycled_owner_from_an_earlier_boot()
    )
    _journal_stalled_retry_reason(store, HOST_RESTART_REPEATED_PRESERVED_REASON)
    ticks_before = store.latest_epic_tick(EPIC).id

    _take_up(store, how)

    assert store.latest_epic_tick(EPIC).id == ticks_before
    # Drive's notifications.
    observations = read_journaled_observations(store.latest_epic_tick(EPIC))
    assert observations is not None
    assert list(journaled_attention_events(EPIC, observations, store=store)) == []
    # The console's needs-you set.
    rows = needs_you.needs_you_set(store, _checkout_config(), now=datetime.now(UTC))
    assert [r for r in rows.entries if r.item_id == ITEM] == []
    # The epic page.
    detail = get_epic_detail(store, EPIC, config=_checkout_config())
    assert detail is not None
    [node] = [n for n in detail.snapshot.nodes if n.id == ITEM]
    assert node.attention is None
    assert node.recovery_command == ""
    # The live stream.
    _cursor, frames = fetch_epic_frames(store, EPIC, {}, config=_checkout_config())
    served = json.dumps([detail.model_dump(mode="json"), frames])
    assert "kew epic continue" not in served
    assert "kew epic retry" not in served


def test_a_plain_failure_serves_retry_as_its_recovery_command(store: Any) -> None:
    """Retry is served on the node, not inferred by a reader from a missing
    attention: a plain terminal failure is nobody's wait, and still retried."""
    from kew.service.epics import get_epic_detail

    _journal_stalled_retry_reason(store, "failed-terminal (exit 1, tests_failed)")

    detail = get_epic_detail(store, EPIC, config=_checkout_config())
    assert detail is not None
    [node] = [n for n in detail.snapshot.nodes if n.id == ITEM]
    assert node.attention is None
    assert node.recovery_command == f"kew epic retry {EPIC} {ITEM}"


def _fail_dispatch(store: Any, claimed: Any, outcome: str) -> None:
    """The claimed continuation's dispatch exits nonzero or raises: the row
    returns to awaiting continuation, and no tick is journaled."""
    from kew.graph.driver.dispatch import resume_continuation

    def _dispatch(*_args: Any) -> Any:
        if outcome == "exception":
            raise RuntimeError("agent launch failed")
        return SimpleNamespace(exit_code=1)

    if outcome == "exception":
        with pytest.raises(RuntimeError, match="agent launch failed"):
            resume_continuation(claimed, store=store, dispatch=_dispatch)
    else:
        resume_continuation(claimed, store=store, dispatch=_dispatch)
    [record] = store.list_continuations(EPIC)
    assert record.state == "awaiting_continuation"


def _claim(store: Any) -> Any:
    """``kew epic continue`` claims the preserved work."""
    claimed = verbs.epic_resume(
        store, epic_ref=EPIC, item_id=ITEM, config=_checkout_config()
    )
    assert claimed.state == "continuing"
    return claimed


@pytest.mark.usefixtures("_pinned_machine")
@pytest.mark.parametrize("outcome", ["exit", "exception"])
@pytest.mark.parametrize(
    "journaled",
    ["failed-terminal (exit 137, host_restart_repeated)", "preserved"],
)
def test_a_failed_continue_returns_the_command_to_continue_without_a_tick(
    store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    journaled: str,
) -> None:
    """The preserved work is still recoverable after a continue whose dispatch
    failed, and the selector still picks it: every surface offers continue
    again, never retry (which refuses it) and never nothing."""
    from kew.graph.driver.attention_journal import (
        journaled_attention_events,
        read_journaled_observations,
    )
    from kew.service.epics import get_epic_detail

    monkeypatch.setenv("KEW_ACTOR", "operator")
    _preserved_through_second_host_death(
        store, tmp_path / "issue-1010", owner=_recycled_owner_from_an_earlier_boot()
    )
    _journal_stalled_retry_reason(
        store,
        HOST_RESTART_REPEATED_PRESERVED_REASON
        if journaled == "preserved"
        else journaled,
    )
    ticks_before = store.latest_epic_tick(EPIC).id

    _fail_dispatch(store, _claim(store), outcome)

    assert store.latest_epic_tick(EPIC).id == ticks_before
    expected = f"kew epic continue {EPIC} {ITEM}"
    # Drive's notifications.
    observations = read_journaled_observations(store.latest_epic_tick(EPIC))
    assert observations is not None
    [event] = journaled_attention_events(EPIC, observations, store=store)
    assert event.reason == HOST_RESTART_REPEATED_PRESERVED_REASON
    bucket = bucket_for(event.reason)
    assert bucket is not None
    assert fill_command(bucket.command, epic=EPIC, item=ITEM) == expected
    # The console's needs-you set.
    rows = needs_you.needs_you_set(store, _checkout_config(), now=datetime.now(UTC))
    commands = {r.command for r in rows.entries if r.item_id == ITEM}
    assert commands == {expected}
    # The epic page.
    detail = get_epic_detail(store, EPIC, config=_checkout_config())
    assert detail is not None
    [node] = [n for n in detail.snapshot.nodes if n.id == ITEM]
    assert node.attention is not None
    assert node.attention.command == expected
    assert node.recovery_command == expected
    assert "kew epic retry" not in json.dumps(detail.model_dump(mode="json"))


def _recovery(frames: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    [payload] = [data for kind, data in frames if kind == "recovery"]
    node: dict[str, Any] = payload["by_node"][ITEM]
    return node


@pytest.mark.usefixtures("_pinned_machine")
@pytest.mark.parametrize("how", ["abandon", "continue"])
def test_a_warm_stream_carries_the_recovery_command_as_it_moves_without_a_tick(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A page held open over the stalled epic gets no new snapshot frame when
    the work is taken up elsewhere: the recovery frame is what moves its
    command — to nothing once the work is taken up, and back to continue when
    a continue's dispatch fails."""
    from kew.service.epics import fetch_epic_frames

    monkeypatch.setenv("KEW_ACTOR", "operator")
    _preserved_through_second_host_death(
        store, tmp_path / "issue-1010", owner=_recycled_owner_from_an_earlier_boot()
    )
    _journal_stalled_retry_reason(store, HOST_RESTART_REPEATED_PRESERVED_REASON)
    config = _checkout_config()
    cursor, frames = fetch_epic_frames(store, EPIC, {}, config=config)
    assert _recovery(frames)["recovery_command"] == f"kew epic continue {EPIC} {ITEM}"
    # Warm and quiet: nothing moved, nothing is re-sent.
    cursor, frames = fetch_epic_frames(store, EPIC, cursor, config=config)
    assert frames == []

    if how == "abandon":
        _take_up(store, "abandon")
    else:
        claimed = _claim(store)

    cursor, frames = fetch_epic_frames(store, EPIC, cursor, config=config)
    assert [kind for kind, _ in frames if kind == "snapshot"] == []
    taken = _recovery(frames)
    assert taken["recovery_command"] == ""
    assert taken["stuck"] != HOST_RESTART_REPEATED_PRESERVED_REASON
    assert "kew epic" not in json.dumps(frames)
    if how == "abandon":
        return

    # The continue's dispatch fails: the same open page gets continue back.
    _fail_dispatch(store, claimed, "exit")
    cursor, frames = fetch_epic_frames(store, EPIC, cursor, config=config)
    assert [kind for kind, _ in frames if kind == "snapshot"] == []
    again = _recovery(frames)
    assert again["stuck"] == HOST_RESTART_REPEATED_PRESERVED_REASON
    assert again["recovery_command"] == f"kew epic continue {EPIC} {ITEM}"
    assert "kew epic retry" not in json.dumps(frames)


#: What ``ui-check``'s ``host-restart-preserved-continue`` serves the browser.
_SCENARIO = (
    Path(__file__).resolve().parents[2]
    / "frontend/fixtures/scenarios/host-restart-preserved-continue.json"
)


def test_the_browser_check_serves_what_the_routes_serve_for_preserved_work(
    store: Any, tmp_path: Path, inbox_client: Any
) -> None:
    """The console's preserved-continuation fixture comes from the ledger.

    The browser check draws Work, Monitor and the Epic page off one committed
    needs-you row and one epic node. Here the driver journals the node whose
    preserved continuation died with its host, and the real
    ``GET /api/dashboard`` and ``GET /api/epics/{ref}`` must serve exactly what
    is committed, field for field — all but the clock's own stamps — so a
    backend that offered ``retry`` fails here and not only in a browser.
    """
    from kew.config import DispatchConfig, ProjectConfig
    from kew.graph.driver.tick import advance
    from kew.run_store import SqliteRunStore
    from kew.serve.app import create_app

    committed = json.loads(_SCENARIO.read_text(encoding="utf-8"))
    epic = committed["epic_ref"]
    _preserved_through_second_host_death(store, tmp_path / "issue-1010", epic=epic)
    advance(
        epic,
        config=_checkout_config(),
        store=store,
        deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        dispatch_sink=_fx.FakeSink(),
        max_parallel=5,
        now=datetime.now(UTC),
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo=_fx.REPO,
    )
    app = create_app(
        lambda: SqliteRunStore(str(tmp_path / "run.db")),
        repo_dir=tmp_path,
        expose=False,
        config=DispatchConfig(project=ProjectConfig(repo="")),
    )
    client = inbox_client(app, loopback_peer=True)

    rows = client.get("/api/dashboard").json()["rows"]
    nodes = client.get(f"/api/epics/{epic}").json()["snapshot"]["nodes"]

    [row] = [r for r in rows if r["item_id"] == ITEM]
    [node] = [n for n in nodes if n["id"] == ITEM]
    command = f"kew epic continue {epic} {ITEM}"
    assert row["command"] == node["attention"]["command"] == command
    assert node["recovery_command"] == command

    def unclocked(served: dict[str, Any], stamp: str) -> dict[str, Any]:
        return {k: v for k, v in served.items() if k != stamp}

    assert unclocked(row, "age") == unclocked(committed["row"], "age")
    assert unclocked(node, "status_since") == unclocked(
        committed["node"], "status_since"
    )
