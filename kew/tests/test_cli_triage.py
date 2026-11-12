"""`kew triage`: the three commands, and which one acts (#2293).

The surface an operator touches. What is held here:

* ``kew triage <item>`` reaches the hidden ``item`` command through
  ``kew.cli._apps._TriageGroup`` while ``queue`` and ``decide`` stay real
  subcommands — the routing that makes the spec's three forms coexist.
* Every refusal is a refusal: the station being off, a non-numeric item, a
  failed ``gh`` read, a missing ledger, no proposal to decide, and an ambiguous
  set of decision flags each exit 2 with a sentence naming what happened. An
  ``unavailable`` proposal exits 1 — "it ran, and here is a finding".
* ``decide`` records WHO. A decision with no identifiable person is refused
  rather than attributed to nobody.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from support.fake_openai import FakeOpenAI, json_completion, unreachable_base_url
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, LemonadeConfig, ProjectConfig, TriageConfig
from kew.run_store import SqliteRunStore

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()

ANSWER = {
    "type": "feature",
    "priority": "p2",
    "complexity": "medium",
    "confidence": 0.86,
    "actionable": True,
    "needs_info": False,
    "questions": [],
    "duplicate_of": None,
    "summary": "a triage station",
}

ISSUE = {
    "number": 2293,
    "title": "a triage station in shadow mode",
    "body": "A classifier proposes; people decide.",
    "labels": [{"name": "kew:triage"}],
}


@pytest.fixture
def server() -> Iterator[Any]:
    fake = FakeOpenAI()
    try:
        yield fake.start
    finally:
        fake.close()


def _config(tmp_path: Path, base_url: str = "", **triage: Any) -> DispatchConfig:
    fields: dict[str, Any] = {"enabled": True, "model": "fake-local-7b"}
    fields.update(triage)
    config = DispatchConfig(
        project=ProjectConfig(name="kew", repo="owner/repo"),
        triage=TriageConfig(**fields),
        agent={
            "lemonade": LemonadeConfig(base_url=base_url or "http://127.0.0.1:1/v1")
        },  # type: ignore[arg-type]
    )
    config.logging.dir = str(tmp_path)
    return config


def _gh(payload: Any = None, returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        ["gh"],
        returncode,
        json.dumps(payload if payload is not None else ISSUE),
        stderr,
    )


def _invoke(config: DispatchConfig, args: list[str], *, gh: Any = None) -> Any:
    reader = gh or (lambda *_a, **_k: _gh())
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.triage.run", reader),
        patch("kew.triage.signals.run", lambda *_a, **_k: _gh([])),
    ):
        return runner.invoke(app, args)


class TestTheBareItemFormRoutes:
    def test_kew_triage_item_records_a_proposal(
        self, tmp_path: Path, server: Any
    ) -> None:
        base_url, _ = server([(200, json_completion(ANSWER))])
        result = _invoke(_config(tmp_path, base_url), ["triage", "2293"])
        assert result.exit_code == 0, result.output
        assert "propose_dispatch" in result.output
        assert "proposes: dispatch (feature, medium, 0.86)" in result.output
        assert "Nothing was applied" in result.output
        assert "kew triage decide 2293" in result.output

    def test_the_item_command_is_hidden_from_help(self, tmp_path: Path) -> None:
        """An operator types ``kew triage 2293``, never ``kew triage item``."""
        result = _invoke(_config(tmp_path), ["triage", "--help"])
        assert "item" not in [
            line.split()[0] for line in result.output.splitlines() if line.strip()
        ]
        assert "queue" in result.output
        assert "decide" in result.output

    def test_queue_is_not_mistaken_for_an_item(self, tmp_path: Path) -> None:
        result = _invoke(_config(tmp_path), ["triage", "queue"])
        assert result.exit_code == 2, result.output
        assert "no ledger" in result.output

    def test_an_unchanged_body_says_the_proposal_still_stands(
        self, tmp_path: Path, server: Any
    ) -> None:
        base_url, _ = server([(200, json_completion(ANSWER))] * 2)
        config = _config(tmp_path, base_url)
        _invoke(config, ["triage", "2293"])
        again = _invoke(config, ["triage", "2293"])
        assert again.exit_code == 0, again.output
        assert "the existing proposal still stands" in again.output


class TestRefusalsAreRefusals:
    def test_the_station_being_off_refuses(self, tmp_path: Path) -> None:
        result = _invoke(_config(tmp_path, enabled=False), ["triage", "2293"])
        assert result.exit_code == 2
        assert "the triage station is off" in result.output

    def test_a_non_numeric_item_refuses(self, tmp_path: Path) -> None:
        result = _invoke(_config(tmp_path), ["triage", "SFD-12"])
        assert result.exit_code == 2
        assert "not a GitHub issue number" in result.output

    def test_a_failed_gh_read_refuses_rather_than_triaging_an_empty_body(
        self, tmp_path: Path
    ) -> None:
        def failing(*_a: Any, **_k: Any) -> Any:
            return _gh(returncode=1, stderr="gh: not authenticated")

        result = _invoke(_config(tmp_path), ["triage", "2293"], gh=failing)
        assert result.exit_code == 2
        assert "could not read issue #2293" in result.output
        assert "not authenticated" in result.output

    def test_an_unreadable_gh_payload_refuses(self, tmp_path: Path) -> None:
        def broken(*_a: Any, **_k: Any) -> Any:
            return subprocess.CompletedProcess(["gh"], 0, "not json", "")

        result = _invoke(_config(tmp_path), ["triage", "2293"], gh=broken)
        assert result.exit_code == 2
        assert "unreadable issue payload" in result.output

    def test_no_repository_configured_refuses(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        config.project.repo = ""
        result = _invoke(config, ["triage", "2293"])
        assert result.exit_code == 2
        assert "no repository configured" in result.output

    def test_an_unavailable_classifier_exits_one_with_a_recorded_row(
        self, tmp_path: Path
    ) -> None:
        result = _invoke(_config(tmp_path, unreachable_base_url()), ["triage", "2293"])
        assert result.exit_code == 1, result.output
        assert "unavailable" in result.output
        assert "needs a person" in result.output
        queue = _invoke(_config(tmp_path), ["triage", "queue"])
        assert "2293: unavailable" in queue.output


class TestSkipEpicNodes:
    """``[triage] skip_epic_nodes`` leaves an epic's own nodes alone."""

    @staticmethod
    def _epic_journal(item_id: str) -> Any:
        class _Tick:
            snapshot = {"nodes": [{"id": item_id, "status": "READY"}]}

        return _Tick()

    def test_an_epic_node_is_skipped(self, tmp_path: Path, server: Any) -> None:
        base_url, script = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)
        with (
            patch.object(SqliteRunStore, "list_epic_refs", lambda _s: ["go-live"]),
            patch.object(
                SqliteRunStore,
                "latest_epic_tick",
                lambda _s, _ref: self._epic_journal("2293"),
            ),
        ):
            result = _invoke(config, ["triage", "2293"])
        assert result.exit_code == 0, result.output
        assert "skipped 2293" in result.output
        assert "node of epic go-live" in result.output
        assert script.requests == [], "no classifier call for a skipped item"

    def test_a_skipped_item_is_never_even_read(
        self, tmp_path: Path, server: Any
    ) -> None:
        """The skip precedes the ``gh`` read: an epic node costs nothing."""
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)
        reads: list[Any] = []

        def recording(*args: Any, **_k: Any) -> Any:
            reads.append(args)
            return _gh()

        with (
            patch.object(SqliteRunStore, "list_epic_refs", lambda _s: ["go-live"]),
            patch.object(
                SqliteRunStore,
                "latest_epic_tick",
                lambda _s, _ref: self._epic_journal("2293"),
            ),
        ):
            result = _invoke(config, ["triage", "2293"], gh=recording)
        assert result.exit_code == 0, result.output
        assert reads == [], "a skipped item is not fetched from GitHub"

    def test_an_item_outside_every_epic_is_triaged(
        self, tmp_path: Path, server: Any
    ) -> None:
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)
        with (
            patch.object(SqliteRunStore, "list_epic_refs", lambda _s: ["go-live"]),
            patch.object(
                SqliteRunStore,
                "latest_epic_tick",
                lambda _s, _ref: self._epic_journal("9999"),
            ),
        ):
            result = _invoke(config, ["triage", "2293"])
        assert result.exit_code == 0, result.output
        assert "propose_dispatch" in result.output

    def test_the_switch_off_triages_an_epic_node(
        self, tmp_path: Path, server: Any
    ) -> None:
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url, skip_epic_nodes=False)
        with (
            patch.object(SqliteRunStore, "list_epic_refs", lambda _s: ["go-live"]),
            patch.object(
                SqliteRunStore,
                "latest_epic_tick",
                lambda _s, _ref: self._epic_journal("2293"),
            ),
        ):
            result = _invoke(config, ["triage", "2293"])
        assert result.exit_code == 0, result.output
        assert "propose_dispatch" in result.output

    def test_an_unreadable_epic_journal_triages_and_says_so(
        self, tmp_path: Path, server: Any
    ) -> None:
        """A skip is a decision; this one would be taken on no evidence."""
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)

        def boom(_self: Any) -> Any:
            raise RuntimeError("no epic table")

        with patch.object(SqliteRunStore, "list_epic_refs", boom):
            result = _invoke(config, ["triage", "2293"])
        assert result.exit_code == 0, result.output
        assert "could not read the epic journal" in result.output
        assert "propose_dispatch" in result.output


class TestTheQueue:
    def test_an_empty_queue_says_so(self, tmp_path: Path, server: Any) -> None:
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)
        _invoke(config, ["triage", "2293"])
        _invoke(config, ["triage", "decide", "2293", "--decline", "--by", "tom"])
        result = _invoke(config, ["triage", "queue"])
        assert result.exit_code == 0
        assert "no proposals are waiting on a person" in result.output

    def test_the_queue_prints_the_command_that_decides_each_row(
        self, tmp_path: Path, server: Any
    ) -> None:
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)
        _invoke(config, ["triage", "2293"])
        result = _invoke(config, ["triage", "queue"])
        assert result.exit_code == 0, result.output
        assert "2293: propose_dispatch" in result.output
        assert "type=feature" in result.output
        assert "confidence=0.86" in result.output
        assert "kew triage decide 2293" in result.output

    def test_a_missing_ledger_refuses(self, tmp_path: Path) -> None:
        result = _invoke(_config(tmp_path / "nowhere"), ["triage", "queue"])
        assert result.exit_code == 2
        assert "no ledger" in result.output


class TestDecide:
    def _propose(self, tmp_path: Path, server: Any) -> DispatchConfig:
        base_url, _ = server([(200, json_completion(ANSWER))])
        config = _config(tmp_path, base_url)
        assert _invoke(config, ["triage", "2293"]).exit_code == 0
        return config

    @pytest.mark.parametrize(
        ("flags", "recorded"),
        [
            (["--dispatch"], "dispatch"),
            (["--ask"], "ask"),
            (["--decline"], "decline"),
            (["--close-duplicate", "812"], "close_duplicate"),
        ],
    )
    def test_each_flag_records_its_resolution(
        self, tmp_path: Path, server: Any, flags: list[str], recorded: str
    ) -> None:
        config = self._propose(tmp_path, server)
        result = _invoke(config, ["triage", "decide", "2293", *flags, "--by", "tom"])
        assert result.exit_code == 0, result.output
        assert f"resolved as {recorded}" in result.output
        assert "by tom" in result.output

    def test_close_duplicate_names_the_duplicate(
        self, tmp_path: Path, server: Any
    ) -> None:
        config = self._propose(tmp_path, server)
        result = _invoke(
            config,
            ["triage", "decide", "2293", "--close-duplicate", "812", "--by", "tom"],
        )
        assert "duplicate of #812" in result.output

    @pytest.mark.parametrize(
        "flags",
        [[], ["--dispatch", "--decline"], ["--ask", "--close-duplicate", "812"]],
        ids=["none", "two", "two-with-a-value"],
    )
    def test_an_ambiguous_decision_is_refused(
        self, tmp_path: Path, server: Any, flags: list[str]
    ) -> None:
        config = self._propose(tmp_path, server)
        result = _invoke(config, ["triage", "decide", "2293", *flags, "--by", "tom"])
        assert result.exit_code == 2
        assert "choose exactly one of" in result.output

    def test_close_duplicate_zero_is_refused(self, tmp_path: Path, server: Any) -> None:
        config = self._propose(tmp_path, server)
        result = _invoke(
            config,
            ["triage", "decide", "2293", "--close-duplicate", "0", "--by", "tom"],
        )
        assert result.exit_code == 2
        assert "needs an issue number" in result.output

    def test_an_item_with_no_proposal_refuses(
        self, tmp_path: Path, server: Any
    ) -> None:
        config = self._propose(tmp_path, server)
        result = _invoke(
            config, ["triage", "decide", "9999", "--dispatch", "--by", "tom"]
        )
        assert result.exit_code == 2
        assert "no unresolved triage proposal for 9999" in result.output
        assert "kew triage queue" in result.output

    def test_a_second_decision_cannot_rewrite_the_first(
        self, tmp_path: Path, server: Any
    ) -> None:
        config = self._propose(tmp_path, server)
        _invoke(config, ["triage", "decide", "2293", "--decline", "--by", "tom"])
        again = _invoke(
            config, ["triage", "decide", "2293", "--dispatch", "--by", "mallory"]
        )
        assert again.exit_code == 2, again.output
        assert "no unresolved triage proposal" in again.output

    def test_the_decider_defaults_to_the_local_user(
        self, tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = self._propose(tmp_path, server)
        monkeypatch.setenv("USER", "tom")
        result = _invoke(config, ["triage", "decide", "2293", "--dispatch"])
        assert result.exit_code == 0, result.output
        assert "by tom" in result.output

    def test_an_unidentifiable_decider_is_refused(
        self, tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolution with nobody behind it is worse than no resolution."""
        config = self._propose(tmp_path, server)
        monkeypatch.setenv("USER", "")
        with patch("getpass.getuser", side_effect=OSError("no passwd entry")):
            result = _invoke(config, ["triage", "decide", "2293", "--dispatch"])
        assert result.exit_code == 2
        assert "cannot tell who you are" in result.output

    def test_decide_works_with_the_station_switched_off(
        self, tmp_path: Path, server: Any
    ) -> None:
        """An operator can always drain a queue they no longer want to grow."""
        config = self._propose(tmp_path, server)
        config.triage.enabled = False
        result = _invoke(
            config, ["triage", "decide", "2293", "--decline", "--by", "tom"]
        )
        assert result.exit_code == 0, result.output

    def test_a_missing_ledger_refuses(self, tmp_path: Path) -> None:
        result = _invoke(
            _config(tmp_path / "nowhere"),
            ["triage", "decide", "2293", "--dispatch", "--by", "tom"],
        )
        assert result.exit_code == 2
        assert "no ledger" in result.output
