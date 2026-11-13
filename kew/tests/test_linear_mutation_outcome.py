"""A 5xx reply to a Linear MUTATION is an unknown outcome, never a retry (#1117).

The write may have committed behind the error, so the client must not send it
again (a retried comment duplicates; a retried claim races a human). These
tests drive the real client and the real ``_default_transport`` with only
``http.client.HTTPSConnection`` faked, and hold the claim to the round-22/23/24
unknown-outcome path: one mutation, ``linear_claim_outcome_unknown`` recorded,
then verification and retention on refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_round21_admission_owner import ITEM
from test_round22_dispatcher_liveness import _claim_scene
from typer.testing import CliRunner

from kew.cli import epic_app
from kew.config import DispatchConfig
from kew.linear.client import LinearClient, LinearUnavailableError, _default_transport
from kew.linear.tracker import LinearTracker
from kew.run_store import SqliteRunStore

FAKE_KEY = "lin_api_mutation_fixture_value"


class _Server:
    """Fake HTTPS connection (and response): an IssueUpdate commits, then the
    edge answers ``write_status``; the claim-state read answers 200."""

    def __init__(self, write_status: int, body: bytes) -> None:
        self.write_status = write_status
        self.body = body
        self.mutations = 0
        self.verifications = 0
        self.issue: dict[str, Any] = {
            "identifier": ITEM,
            "assignee": None,
            "state": {"id": "ready", "name": "Todo"},
        }

    def __call__(self, host: str, timeout: float) -> _Server:
        return self

    def connect(self) -> None:
        return None

    def request(self, method: str, path: str, body: bytes, headers: Any) -> None:
        self._request = json.loads(body)

    def getresponse(self) -> _Server:
        return self

    @property
    def status(self) -> int:
        return self.write_status if "IssueUpdate" in self._request["query"] else 200

    def read(self) -> bytes:
        query = self._request["query"]
        if "IssueUpdate" in query:
            self.mutations += 1
            fields = self._request["variables"]["input"]
            # The worst case: Linear committed the write, then the edge failed.
            self.issue = {
                "identifier": ITEM,
                "assignee": {"id": fields.get("assigneeId")},
                "state": {"id": fields["stateId"], "name": "In Progress"},
                "updatedAt": f"version-{self.mutations}",
            }
            return self.body
        if "IssueClaimState" in query:
            self.verifications += 1
            return json.dumps({"data": {"issue": self.issue}}).encode()
        raise AssertionError(f"unexpected query: {query[:60]}")

    def close(self) -> None:
        return None


_REPLIES = [
    pytest.param(500, b"<html>500 Internal Server Error</html>", id="http-500"),
    pytest.param(502, b"<html>502 Bad Gateway</html>", id="http-502"),
    pytest.param(520, b"error code: 520", id="cloudflare-520"),
    pytest.param(429, b"<html>429 Too Many Requests</html>", id="edge-429-html"),
]


@pytest.mark.parametrize(("status", "body"), _REPLIES)
def test_claim_5xx_reply_is_not_retried_and_records_unknown_outcome(
    admission_owner, tmp_path, monkeypatch, status, body
):
    server = _Server(status, body)
    monkeypatch.setattr("http.client.HTTPSConnection", server)
    monkeypatch.setattr("kew.cli.epic._warn", lambda *_a, **_k: None)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, _default_transport)
        with pytest.raises(LinearUnavailableError) as raised:
            claim.admit("ambiguous")

        assert server.mutations == 1, "a write with an unknown outcome was resent"
        client._sleep.assert_not_called()
        assert raised.value.ambiguous is True
        assert raised.value.write_outcome_unknown is True
        assert claim.outcome_unknown
        assert len(store.list_audit_events(event="linear_claim_outcome_unknown")) == 1

        # Refusal takes the existing path: verify once, then retain.
        claim.retain("claim outcome unknown")
        assert server.verifications == 1
        assert server.mutations == 1
        [completed] = store.list_audit_events(event="linear_claim_completed")
        assert completed.payload["source"] == "verification"
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1


def _config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path)
    cfg.tracker.type = "linear"
    cfg.tracker.linear.team = "SFD"
    return cfg


def test_tick_write_with_unknown_outcome_exits_2_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick whose Linear close gets a 500 reply ends classified, exit 2."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", FAKE_KEY)
    server = _Server(500, f"<html>500 {FAKE_KEY}</html>".encode())
    monkeypatch.setattr("http.client.HTTPSConnection", server)
    cfg = _config(tmp_path)
    sleeps: list[float] = []
    tracker = LinearTracker(cfg, LinearClient(FAKE_KEY, sleep=sleeps.append))
    tracker._states = [{"id": "done", "name": "Done", "type": "completed"}]

    def tick(*_args: Any, **_kwargs: Any) -> None:
        tracker.close(ITEM)  # the merge pass's Linear write

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.epic._driver_repo_host", return_value="github.com"),
        patch("kew.cli.advance", side_effect=tick),
    ):
        invoked = CliRunner().invoke(epic_app, ["advance", "payments"])

    assert invoked.exit_code == 2, invoked.output
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit)
    assert "Traceback" not in invoked.output
    assert "epic payments: linear-outcome-unknown — " in invoked.output
    assert "Check the item's state" in " ".join(invoked.output.split())
    assert FAKE_KEY not in "".join(invoked.output.split())
    assert server.mutations == 1
    assert sleeps == []
