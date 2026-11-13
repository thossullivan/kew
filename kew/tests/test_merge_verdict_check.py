"""``kew merge check``: the one merge verdict rule an operator's hand merge asks.

Each test writes attested ``adversarial_review_verdict`` rows into a real ledger
and reads the answer off the CLI's exit code, its output and the audit trail.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.config import ReviewConfig
from kew.merge_verdict import MERGE_VERDICT_OVERRIDE_EVENT, check_merge_verdict
from kew.review_audit import emit_review_verdict
from kew.run_store import SqliteRunStore

REPO = "o/r"
ITEM = "42"
PR = 7
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    db = tmp_path / "dispatch.db"
    SqliteRunStore(str(db)).close()
    return db


@pytest.fixture
def project(tmp_path: Path, ledger: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{ledger.parent}"\n'
    )
    return root


def _verdict(ledger: Path, verdict: str, *, head: str = HEAD, **extra: Any) -> None:
    with SqliteRunStore(str(ledger)) as store:
        emit_review_verdict(
            store,
            ReviewConfig(),
            pr_number=PR,
            item_id=ITEM,
            head_sha=head,
            review_round=1,
            verdict=verdict,
            **extra,
        )


def _check(project: Path, *args: str) -> Any:
    return CliRunner().invoke(
        app, ["--project", str(project), "merge", "check", str(PR), *args]
    )


def _overrides(ledger: Path) -> list[Any]:
    with SqliteRunStore(str(ledger)) as store:
        return store.list_audit_events(None, event=MERGE_VERDICT_OVERRIDE_EVENT)


def test_a_needs_changes_verdict_on_the_head_refuses_and_names_it(
    project: Path, ledger: Path
) -> None:
    _verdict(ledger, "NEEDS_CHANGES")

    result = _check(project, HEAD)

    assert result.exit_code == 1
    assert "NEEDS_CHANGES" in result.output
    assert _overrides(ledger) == []


def test_a_kew_fault_landing_after_needs_changes_does_not_hide_it(
    project: Path, ledger: Path
) -> None:
    _verdict(ledger, "NEEDS_CHANGES")
    _verdict(ledger, "UNPARSEABLE", error_category="review_failed_no_verdict")
    _verdict(ledger, "DEGRADED")

    result = _check(project, HEAD)

    assert result.exit_code == 1
    assert "NEEDS_CHANGES" in result.output


def test_a_newer_ready_verdict_on_the_head_passes(project: Path, ledger: Path) -> None:
    _verdict(ledger, "NEEDS_CHANGES")
    _verdict(ledger, "READY")

    result = _check(project, HEAD)

    assert result.exit_code == 0, result.output
    assert "READY" in result.output


def test_an_override_reason_passes_and_attests_actor_head_and_reason(
    project: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEW_ACTOR", raising=False)
    monkeypatch.setenv("LOGNAME", "tom")
    _verdict(ledger, "NEEDS_CHANGES")

    result = _check(project, HEAD, "--override-reason", "finding is a false positive")

    assert result.exit_code == 0, result.output
    (event,) = _overrides(ledger)
    assert (event.actor_type, event.actor_id) == ("human", "tom")
    assert event.payload["actor"] == "tom"
    assert event.payload["head_sha"] == HEAD
    assert event.payload["reason"] == "finding is a false positive"
    assert event.payload["verdict"] == "NEEDS_CHANGES"
    assert event.payload["pr"] == PR


def test_a_blank_override_reason_refuses_and_attests_nothing(
    project: Path, ledger: Path
) -> None:
    _verdict(ledger, "NEEDS_CHANGES")

    result = _check(project, HEAD, "--override-reason", "  ")

    assert result.exit_code == 2
    assert _overrides(ledger) == []


def test_an_unknown_verdict_refuses_even_with_an_override(
    project: Path, ledger: Path
) -> None:
    _verdict(ledger, "MAYBE")

    plain = _check(project, HEAD)
    overridden = _check(project, HEAD, "--override-reason", "ship it")

    assert plain.exit_code == 1 and "MAYBE" in plain.output
    assert overridden.exit_code == 1
    assert _overrides(ledger) == []


@pytest.mark.parametrize("head", ["a" * 12, "z" * 40, ""])
def test_a_malformed_head_refuses(project: Path, ledger: Path, head: str) -> None:
    result = _check(project, head)

    assert result.exit_code == 2
    assert "malformed-head" in result.output


def test_a_missing_ledger_refuses(project: Path, ledger: Path) -> None:
    ledger.unlink()

    result = _check(project, HEAD)

    assert result.exit_code == 2
    assert "unreadable-ledger" in result.output
    assert not ledger.exists()


def test_a_corrupt_ledger_refuses(project: Path, ledger: Path) -> None:
    ledger.write_bytes(b"this is not a sqlite database" * 64)

    result = _check(project, HEAD)

    assert result.exit_code == 2
    assert "cannot be inspected" in result.output


def test_a_verdict_on_another_head_is_ignored(project: Path, ledger: Path) -> None:
    _verdict(ledger, "NEEDS_CHANGES", head=OTHER_HEAD)

    result = _check(project, HEAD)

    assert result.exit_code == 0, result.output


def test_no_verdict_passes_an_operator_check(project: Path, ledger: Path) -> None:
    result = _check(project, HEAD)

    assert result.exit_code == 0, result.output
    assert "no review verdict" in result.output


def test_a_needs_full_verdict_refuses_with_its_reason(
    project: Path, ledger: Path
) -> None:
    """NEEDS_FULL is a kew verdict: a delta READY that still owes a full round."""
    _verdict(ledger, "NEEDS_FULL", review_mode="delta")

    result = _check(project, HEAD)

    assert result.exit_code == 1
    assert "needs-full" in result.output
    assert (
        "a review round that still owes an independent full round at this head"
        in result.output
    )
    assert "not a kew review verdict" not in result.output
    assert _overrides(ledger) == []


def test_an_override_reason_passes_a_needs_full_verdict_and_attests_it(
    project: Path, ledger: Path
) -> None:
    _verdict(ledger, "NEEDS_FULL", review_mode="delta")

    result = _check(project, HEAD, "--override-reason", "the delta was the whole PR")

    assert result.exit_code == 0, result.output
    (event,) = _overrides(ledger)
    assert event.payload["verdict"] == "NEEDS_FULL"
    assert event.payload["head_sha"] == HEAD
    assert event.payload["reason"] == "the delta was the whole PR"


def test_the_override_names_the_kew_actor_when_one_is_set(
    project: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "release-captain")
    monkeypatch.setenv("LOGNAME", "tom")
    _verdict(ledger, "NEEDS_CHANGES")

    result = _check(project, HEAD, "--override-reason", "hotfix")

    assert result.exit_code == 0, result.output
    (event,) = _overrides(ledger)
    assert (event.actor_type, event.actor_id) == ("human", "release-captain")
    assert event.payload["actor"] == "release-captain"


def test_a_needs_changes_carrying_an_unnamed_category_still_refuses(
    project: Path, ledger: Path
) -> None:
    """Only the review stage's named fault categories mark a row as a kew fault;
    any other ``error_category`` leaves the verdict standing."""
    _verdict(ledger, "NEEDS_CHANGES", error_category="reviewer_note")

    result = _check(project, HEAD)

    assert result.exit_code == 1
    assert "NEEDS_CHANGES" in result.output


def test_a_ready_row_naming_a_fault_category_does_not_hide_needs_changes(
    project: Path, ledger: Path
) -> None:
    _verdict(ledger, "NEEDS_CHANGES")
    _verdict(ledger, "READY", error_category="review_failed_no_verdict")

    result = _check(project, HEAD)

    assert result.exit_code == 1
    assert "NEEDS_CHANGES" in result.output


def test_the_fault_categories_are_the_review_stages_crash_holds() -> None:
    """The rule skips exactly the categories the review stage holds a failed
    round under, so a new one cannot land without the rule knowing it."""
    from kew.graph.driver.review_stage import _CRASH_HOLDS
    from kew.merge_verdict import REVIEW_FAULT_CATEGORIES

    assert frozenset(_CRASH_HOLDS) == REVIEW_FAULT_CATEGORIES


def test_a_verdict_row_whose_payload_does_not_decode_refuses_closed(
    project: Path, ledger: Path
) -> None:
    """A corrupt row could be the newest verdict on any head, so it refuses
    every head, overridden or not, and names the row."""
    _verdict(ledger, "NEEDS_CHANGES", head=OTHER_HEAD)
    conn = sqlite3.connect(ledger)
    try:
        (event_id,) = conn.execute(
            "SELECT id FROM audit_events WHERE event = 'adversarial_review_verdict'"
        ).fetchone()
        conn.execute(
            "UPDATE audit_events SET payload = ? WHERE id = ?", ("{not json", event_id)
        )
        conn.commit()
    finally:
        conn.close()

    plain = _check(project, HEAD)
    overridden = _check(project, HEAD, "--override-reason", "ship it")

    assert plain.exit_code == 2
    assert "corrupt-verdict" in plain.output
    assert f"#{event_id}" in plain.output
    assert overridden.exit_code == 2
    assert _overrides(ledger) == []


@pytest.mark.parametrize("suffix", ["\n", " "], ids=["newline", "space"])
def test_a_head_with_trailing_bytes_is_malformed(ledger: Path, suffix: str) -> None:
    with SqliteRunStore(str(ledger)) as store:
        result = check_merge_verdict(store, HEAD + suffix)

    assert result.refusal == "malformed-head"
