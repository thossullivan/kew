"""The epic-archive state read off the audit chain (#2206).

`kew epic archive` records its decision as an attested audit event and nothing
else: no column, no row, no deletion. So the archived set is DERIVED — the
latest `epic_archived`/`epic_unarchived` event per epic ref wins — and this
file pins that derivation, because four readers depend on it (the Epics list,
the needs-you count, `kew epic run --all`, and the CLI's own refusals).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, ProjectConfig
from kew.epic_archive import (
    EPIC_ARCHIVED_EVENT,
    EPIC_UNARCHIVED_EVENT,
    archive_states,
    archived_epic_refs,
    is_archived,
)
from kew.run_store import SqliteRunStore
from kew.service import verbs


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


def _event(store: Any, event: str, epic_ref: str, reason: str = "") -> int:
    return store.append_audit_event(
        timestamp="2026-09-29T00:00:00+00:00",
        event=event,
        actor_type="human",
        actor_id="owner",
        payload={"epic_ref": epic_ref, "reason": reason},
    )


def test_no_events_means_nothing_is_archived(store: Any) -> None:
    assert archived_epic_refs(store) == frozenset()
    assert archive_states(store) == {}
    assert is_archived(store, "epic:anything") is False


def test_an_archive_event_archives_its_epic(store: Any) -> None:
    _event(store, EPIC_ARCHIVED_EVENT, "epic:old", reason="shipped in July")

    assert archived_epic_refs(store) == frozenset({"epic:old"})
    assert is_archived(store, "epic:old") is True
    assert is_archived(store, "epic:other") is False

    state = archive_states(store)["epic:old"]
    assert state.archived is True
    assert state.reason == "shipped in July"
    assert state.actor == "owner"
    assert state.at == "2026-09-29T00:00:00+00:00"


def test_the_latest_event_wins_and_nothing_is_deleted(store: Any) -> None:
    """Unarchive does not remove the archive event — it outranks it."""
    _event(store, EPIC_ARCHIVED_EVENT, "epic:a")
    _event(store, EPIC_UNARCHIVED_EVENT, "epic:a", reason="back on the roster")

    assert archived_epic_refs(store) == frozenset()
    assert is_archived(store, "epic:a") is False
    assert archive_states(store)["epic:a"].archived is False
    # Both events are still on the chain.
    assert len(store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)) == 1

    _event(store, EPIC_ARCHIVED_EVENT, "epic:a", reason="abandoned after all")
    assert is_archived(store, "epic:a") is True
    assert archive_states(store)["epic:a"].reason == "abandoned after all"


def test_other_epics_events_do_not_cross_over(store: Any) -> None:
    _event(store, EPIC_ARCHIVED_EVENT, "epic:a")
    _event(store, EPIC_UNARCHIVED_EVENT, "epic:b")

    assert archived_epic_refs(store) == frozenset({"epic:a"})


def test_an_unrelated_event_is_never_read_as_an_archive(store: Any) -> None:
    """The reader keys on each row's own event name, so a store (or fake) that
    ignores the `event=` filter cannot turn an approval into an archive."""
    store.append_audit_event(
        timestamp="2026-09-29T00:00:00+00:00",
        event="approval_hold",
        actor_type="human",
        actor_id="owner",
        payload={"epic_ref": "epic:a"},
    )
    assert archived_epic_refs(store) == frozenset()


def test_an_event_without_an_epic_ref_is_skipped_not_guessed(store: Any) -> None:
    store.append_audit_event(
        timestamp="2026-09-29T00:00:00+00:00",
        event=EPIC_ARCHIVED_EVENT,
        actor_type="human",
        actor_id="owner",
        payload={"reason": "no ref recorded"},
    )
    assert archived_epic_refs(store) == frozenset()


# --------------------------------------------------------------------------- #
# The operator verb: attest, then the state follows (#2206)
# --------------------------------------------------------------------------- #


def _known_epic(store: Any, ref: str) -> None:
    store.begin_epic_tick(ref, "2026-07-02T00:00:00+00:00")


def test_the_verb_attests_the_decision_and_archives(store: Any) -> None:
    _known_epic(store, "epic:old")
    state = verbs.set_epic_archived(
        store,
        epic_ref="epic:old",
        archived=True,
        reason="shipped in July",
        config=DispatchConfig(project=ProjectConfig(repo="owner/repo")),
    )
    assert state.archived is True
    assert state.reason == "shipped in July"

    events = store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)
    assert len(events) == 1
    assert events[0].payload["epic_ref"] == "epic:old"
    assert events[0].payload["reason"] == "shipped in July"
    assert events[0].payload["source"] == "cli"
    assert events[0].actor_type == "human"
    assert events[0].repo == "owner/repo"
    assert is_archived(store, "epic:old") is True


def test_the_verb_returns_the_state_the_other_readers_derive(store: Any) -> None:
    """The returned state is read back off the chain, not reconstructed from
    what the verb meant to write — so its timestamp IS the event's, and a
    caller can never report a retirement the Epics list disagrees with."""
    _known_epic(store, "epic:old")
    state = verbs.set_epic_archived(
        store,
        epic_ref="epic:old",
        archived=True,
        reason="  padded  ",
        config=DispatchConfig(),
    )
    event = store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)[0]
    assert state.at == event.timestamp
    assert state.actor == event.actor_id
    assert state.reason == "padded"
    assert archive_states(store)["epic:old"] == state


def test_the_verb_unarchives_with_its_own_event(store: Any) -> None:
    _known_epic(store, "epic:old")
    config = DispatchConfig()
    verbs.set_epic_archived(
        store, epic_ref="epic:old", archived=True, reason="", config=config
    )
    state = verbs.set_epic_archived(
        store, epic_ref="epic:old", archived=False, reason="back on", config=config
    )
    assert state.archived is False
    assert len(store.list_audit_events(limit=None, event=EPIC_UNARCHIVED_EVENT)) == 1
    assert is_archived(store, "epic:old") is False


def test_the_verb_refuses_an_unknown_epic(store: Any) -> None:
    with pytest.raises(LookupError, match="unknown epic"):
        verbs.set_epic_archived(
            store,
            epic_ref="epic:never-driven",
            archived=True,
            reason="",
            config=DispatchConfig(),
        )
    assert store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT) == []


@pytest.mark.parametrize("archived", [True, False])
def test_the_verb_refuses_a_no_op_rather_than_attesting_one(
    store: Any, archived: bool
) -> None:
    """A second archive is not a second decision. Attesting it would put a
    retirement on the chain that changed nothing, and report a change that did
    not happen."""
    _known_epic(store, "epic:old")
    config = DispatchConfig()
    if archived:
        verbs.set_epic_archived(
            store, epic_ref="epic:old", archived=True, reason="", config=config
        )
    with pytest.raises(verbs.EpicArchiveDenied) as exc:
        verbs.set_epic_archived(
            store, epic_ref="epic:old", archived=archived, reason="", config=config
        )
    assert ("already archived" if archived else "not archived") in str(exc.value)
    assert len(store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)) == (
        1 if archived else 0
    )


# --------------------------------------------------------------------------- #
# The CLI: `kew epic archive` / `kew epic unarchive` (#2206)
# --------------------------------------------------------------------------- #

runner = CliRunner()


def _cli_config(
    tmp_path: Path, refs: tuple[str, ...] = ("epic:old",)
) -> DispatchConfig:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as s:
        for ref in refs:
            s.begin_epic_tick(ref, "2026-07-02T00:00:00+00:00")
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    return cfg


def _ledger(tmp_path: Path) -> Any:
    return SqliteRunStore(str(tmp_path / "logs" / "dispatch.db"))


@patch("kew.cli.load_config")
def test_cli_archive_records_the_event_with_its_reason(
    load_cfg: Any, tmp_path: Path
) -> None:
    load_cfg.return_value = _cli_config(tmp_path)

    result = runner.invoke(
        app, ["epic", "archive", "epic:old", "--reason", "shipped in July"]
    )

    assert result.exit_code == 0, result.output
    assert "epic:old" in result.output
    assert "archived" in result.output
    with _ledger(tmp_path) as store:
        events = store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)
        assert len(events) == 1
        assert events[0].payload == {
            "epic_ref": "epic:old",
            "reason": "shipped in July",
            "source": "cli",
        }
        assert is_archived(store, "epic:old") is True


@patch("kew.cli.load_config")
def test_cli_archive_needs_no_reason(load_cfg: Any, tmp_path: Path) -> None:
    load_cfg.return_value = _cli_config(tmp_path)

    result = runner.invoke(app, ["epic", "archive", "epic:old"])

    assert result.exit_code == 0, result.output
    with _ledger(tmp_path) as store:
        assert is_archived(store, "epic:old") is True


@patch("kew.cli.load_config")
def test_cli_unarchive_returns_the_epic_to_the_roster(
    load_cfg: Any, tmp_path: Path
) -> None:
    load_cfg.return_value = _cli_config(tmp_path)
    assert runner.invoke(app, ["epic", "archive", "epic:old"]).exit_code == 0

    result = runner.invoke(
        app, ["epic", "unarchive", "epic:old", "--reason", "back on the roster"]
    )

    assert result.exit_code == 0, result.output
    with _ledger(tmp_path) as store:
        events = store.list_audit_events(limit=None, event=EPIC_UNARCHIVED_EVENT)
        assert len(events) == 1
        assert events[0].payload["reason"] == "back on the roster"
        assert is_archived(store, "epic:old") is False
        # Nothing is deleted: the archive event is still on the chain.
        assert len(store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)) == 1


@patch("kew.cli.load_config")
def test_cli_archive_refuses_a_no_op(load_cfg: Any, tmp_path: Path) -> None:
    load_cfg.return_value = _cli_config(tmp_path)
    assert runner.invoke(app, ["epic", "archive", "epic:old"]).exit_code == 0

    result = runner.invoke(app, ["epic", "archive", "epic:old"])

    assert result.exit_code == 1, result.output
    assert "already archived" in result.output
    with _ledger(tmp_path) as store:
        assert len(store.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)) == 1


@patch("kew.cli.load_config")
def test_cli_unarchive_refuses_an_epic_that_is_not_archived(
    load_cfg: Any, tmp_path: Path
) -> None:
    load_cfg.return_value = _cli_config(tmp_path)

    result = runner.invoke(app, ["epic", "unarchive", "epic:old"])

    assert result.exit_code == 1, result.output
    assert "not archived" in result.output


@patch("kew.cli.load_config")
def test_cli_archive_refuses_an_unknown_epic(load_cfg: Any, tmp_path: Path) -> None:
    load_cfg.return_value = _cli_config(tmp_path)

    result = runner.invoke(app, ["epic", "archive", "epic:typo"])

    assert result.exit_code == 2, result.output
    assert "unknown epic" in result.output


@patch("kew.cli.load_config")
def test_cli_archive_refuses_without_a_ledger(load_cfg: Any, tmp_path: Path) -> None:
    """No ledger means no known epics — say so rather than writing one."""
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(tmp_path / "logs")
    load_cfg.return_value = cfg

    result = runner.invoke(app, ["epic", "archive", "epic:old"])

    assert result.exit_code == 2, result.output
    assert "no ledger" in result.output
