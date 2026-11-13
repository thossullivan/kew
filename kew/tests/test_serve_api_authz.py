"""Auth coverage for read routers (audit 2026-07-01 F7; delta audit N9).

Every read these routers serve — including the SSE stream of raw agent stdout
and source diffs — must go through read_guard: session-gated under --expose,
and session-gated for non-loopback peers even when not exposed.

N9 (delta audit 2026-07-22): the ``/api/report/*`` and ``/api/fleet/status``
reads were guarded only by convention — no test proved their read_guard
contract. They are pinned into READ_PATHS below so dropping the dependency on
either router fails this suite (401 short-circuits at the guard, before the
handler touches the store).
"""

import asyncio
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.proxied_stream import read_proxied_stream, session_clock

from kew.run_store_artifacts import StoredArtifact
from kew.sensitive_prompt import (
    PROMPT_ARTIFACT_KIND,
    STORAGE_LOCAL,
    default_root,
    retain_prompt,
)
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.serve.sse import event_stream, journal_stream

READ_PATHS = [
    "/api/meta",
    "/api/runs",
    "/api/runs/r1/timeline",
    "/api/runs/r1/events?replay=true",
    # I17 canonical evidence reads: same read_guard contract.
    "/api/runs/r1/flight",
    "/api/runs/r1/journal",
    # The run page's forward-paged ledger read (C3/#2300): the run's own audit
    # events are a read like any other, so dropping the runs router's
    # dependency must fail here too.
    "/api/runs/r1/audit",
    "/api/loops",
    "/api/loops/l1",
    "/api/loops/l1/audit",
    # Report + fleet reads (N9): same read_guard contract as the runs router.
    # The guard 401s before the handler runs, so the FakeStore needs no
    # scorecard/fleet support to exercise the unauthorized matrices.
    "/api/report/models",
    "/api/report/spend",
    "/api/fleet/status",
    # The model catalog the console picks from (#2542).
    "/api/models",
]


class FakeRecord:
    def __init__(self, run_id, log_file):
        self.run_id = run_id
        self.issue_num = 312
        self.title = "rate-limit headers"
        self.runner_name = "claude-code"
        self.model = "claude-opus-4-8"
        self.status = "success"
        self.estimated_cost_usd = 0.006
        self.started_at = None
        self.finished_at = None
        self.log_file = log_file


class FakeStore:
    def __init__(self, records):
        self._records = records

    def list_all(self):
        return list(self._records)


_PROMPT = "SYSTEM: compiled\nSECRET-PROMPT-BYTES-do-not-leak\n"
_STAMP = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class PromptStore(FakeStore):
    """A FakeStore that also indexes one retained prompt, as dispatch does."""

    def __init__(self, records, root: Path):
        super().__init__(records)
        self._ref = retain_prompt(
            default_root(root),
            run_id="r1",
            attempt_number=1,
            text=_PROMPT,
            now=_STAMP,
        )

    def list_artifacts(self, *, run_id=None, kind=None):
        return [
            StoredArtifact(
                run_id="r1",
                attempt_number=1,
                kind=PROMPT_ARTIFACT_KIND,
                storage=STORAGE_LOCAL,
                digest=self._ref.digest,
                byte_length=self._ref.byte_length,
                locator=self._ref.locator,
                created_at=_STAMP.isoformat(),
            )
        ]


def _mk_app(tmp_path: Path, *, expose: bool, sessions: SessionStore | None = None):
    (tmp_path / "r1.log").write_text("log\n")
    (tmp_path / "r1.events.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"ts": 1.0, "type": "started", "issue": 312},
                # terminal event so an (unguarded) SSE stream ends instead of
                # following the file for the 30-min cap — keeps RED runs finite
                {"ts": 2.0, "type": "finished", "exit_code": 0},
            ]
        )
        + "\n"
    )
    store = PromptStore([FakeRecord("r1", tmp_path / "r1.log")], tmp_path)
    return create_app(
        lambda: nullcontext(store),
        repo_dir=tmp_path,
        expose=expose,
        sessions=sessions or SessionStore(),
    )


def _get_status(client, path: str) -> int:
    # The events endpoint is SSE: an unguarded stream never ends, so a plain
    # .get() would block draining it. stream() reads status without the body.
    with client.stream("GET", path) as r:
        return r.status_code


@pytest.mark.parametrize("path", READ_PATHS)
def test_exposed_reads_require_session(tmp_path: Path, inbox_client, path: str):
    """Under --expose, unauthenticated reads must 401 — even from loopback."""
    c = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert _get_status(c, path) == 401


@pytest.mark.parametrize("path", READ_PATHS)
def test_non_loopback_reads_require_session(tmp_path: Path, inbox_client, path: str):
    """Not exposed, but a non-loopback peer: reads must 401 without a session."""
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=False)
    assert _get_status(c, path) == 401


def test_loopback_reads_stay_open_when_not_exposed(tmp_path: Path, inbox_client):
    """The local single-operator flow is unchanged (read_guard loopback rule)."""
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    assert c.get("/api/runs").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Real-IP",
        "Via",
        "X-Forwarded-Server",
        "X-Forwarded-Port",
    ],
)
def test_a_proxied_loopback_read_of_the_runs_list_needs_a_session(
    tmp_path: Path, inbox_client, header: str
):
    """A same-host reverse proxy reaches kew from loopback; the header it adds
    turns the loopback free read off on a real API route, not only a stub."""
    sessions = SessionStore()
    c = inbox_client(
        _mk_app(tmp_path, expose=False, sessions=sessions), loopback_peer=True
    )
    proxied = {header: "1.1 proxy.example"}
    assert c.get("/api/runs", headers=proxied).status_code == 401
    sid, _ = sessions.create()
    c.cookies.set("kew_session", sid)
    assert c.get("/api/runs", headers=proxied).status_code == 200


# Real wall clock (the stream's deadline). Serial phase: see tests/conftest.py.
@pytest.mark.serial
@pytest.mark.asyncio
async def test_a_proxied_run_stream_closes_when_its_session_expires(
    tmp_path: Path,
) -> None:
    """The opening gate asked a proxied loopback reader for a session, so every
    poll after it must too: an expired session ends the stream, not the cap."""
    # create + the opening gate + the first poll see a live session.
    sessions = SessionStore(ttl_seconds=100.0, now=session_clock(3, expired_at=101.0))
    app = _mk_app(tmp_path, expose=False, sessions=sessions)
    # No terminal event: only the session re-check can end this stream.
    (tmp_path / "r1.events.jsonl").write_text('{"ts":1.0,"type":"started"}\n')
    sid, _ = sessions.create()

    response, open_s = await read_proxied_stream(
        app, "/api/runs/r1/events?replay=true", session_id=sid
    )

    assert response.status_code == 200
    assert '"started"' in response.text
    assert open_s < 5.0, "the stream outlived its session"


# Real wall clock. Serial phase: see tests/conftest.py.
@pytest.mark.serial
@pytest.mark.asyncio
async def test_run_events_stream_ends_on_session_revocation(tmp_path: Path) -> None:
    """Mid-stream session revocation terminates the SSE within one poll tick."""
    events_file = tmp_path / "run.events.jsonl"
    events_file.write_text('{"ts":1.0,"type":"started","issue":1}\n')

    authorized: dict[str, bool] = {"value": True}

    def _is_authorized() -> bool:
        return authorized["value"]

    frames: list[str] = []

    async def _consume() -> None:
        async for frame in event_stream(
            events_file,
            replay=True,
            poll_s=0.01,
            is_authorized=_is_authorized,
        ):
            frames.append(frame)
            # Revoke after first frame — next iteration must detect and stop.
            authorized["value"] = False

    await asyncio.wait_for(_consume(), timeout=1.0)
    assert len(frames) == 1
    assert '"started"' in frames[0]


@pytest.mark.asyncio
async def test_run_events_quoted_terminal_does_not_end_stream(tmp_path: Path) -> None:
    """A "type":"failed" nested inside a payload value must not terminate the stream.

    Substring matching would falsely trigger on the first line; JSON parsing
    checks only the top-level type field.
    """
    events_file = tmp_path / "run.events.jsonl"
    # First line: an activity event whose nested sub-object contains
    # "type":"failed" — old substring check would falsely terminate here.
    events_file.write_text(
        '{"ts":1.0,"type":"activity","meta":{"type":"failed","detail":"x"}}\n'
        '{"ts":2.0,"type":"finished","exit_code":0}\n'
    )

    frames: list[str] = []
    async for frame in event_stream(events_file, replay=True, poll_s=0.01):
        frames.append(frame)

    assert len(frames) == 2
    assert '"activity"' in frames[0]
    assert '"finished"' in frames[1]


# --------------------------------------------------------------------------- #
# I17 — the protected prompt boundary and the journal stream
# --------------------------------------------------------------------------- #


def _session_client(app, inbox_client, *, loopback: bool = True):
    sid, _ = app.state.sessions.create()
    client = inbox_client(app, loopback_peer=loopback)
    client.cookies.set("kew_session", sid)
    return client


class TestTheProtectedPromptIsNotAnOrdinaryRead:
    def test_a_loopback_peer_still_needs_a_session(self, tmp_path: Path, inbox_client):
        """read_guard's loopback exemption does NOT extend to the prompt route."""
        app = _mk_app(tmp_path, expose=False)
        client = inbox_client(app, loopback_peer=True)

        assert client.get("/api/runs").status_code == 200  # ordinary read: open
        assert client.get("/api/runs/r1/prompt").status_code == 401

    def test_it_is_disabled_under_expose_even_with_a_real_session(
        self, tmp_path: Path, inbox_client
    ):
        app = _mk_app(tmp_path, expose=True)
        client = _session_client(app, inbox_client)

        assert client.get("/api/runs").status_code == 200  # session is real
        assert client.get("/api/runs/r1/prompt").status_code == 403

    def test_it_is_refused_under_expose_without_a_session(
        self, tmp_path: Path, inbox_client
    ):
        c = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
        assert c.get("/api/runs/r1/prompt").status_code in (401, 403)

    def test_a_revoked_session_stops_reading_the_prompt(
        self, tmp_path: Path, inbox_client
    ):
        app = _mk_app(tmp_path, expose=False)
        sid, _ = app.state.sessions.create()
        client = inbox_client(app, loopback_peer=True)
        client.cookies.set("kew_session", sid)

        app.state.sessions.delete(sid)  # a restart clears the in-process store

        assert client.get("/api/runs/r1/prompt").status_code == 401


# Real wall clock. Serial phase: see tests/conftest.py.
@pytest.mark.serial
@pytest.mark.asyncio
async def test_journal_stream_ends_on_session_revocation() -> None:
    """Mid-stream revocation terminates the journal within one poll tick."""
    authorized = {"value": True}
    served: list[str] = []

    def _fetch(after_seq: int) -> list[dict]:
        return [{"seq": after_seq + 1, "kind": "evidence.receipt_fact"}]

    async def _consume() -> None:
        async for frame in journal_stream(
            _fetch, poll_s=0.01, is_authorized=lambda: authorized["value"]
        ):
            served.append(frame)
            authorized["value"] = False

    await asyncio.wait_for(_consume(), timeout=1.0)
    assert len(served) == 1


@pytest.mark.asyncio
async def test_journal_stream_ends_on_a_terminal_event() -> None:
    """A terminal attestation closes the run's evidence — and the stream."""
    rows = [
        {"seq": 1, "kind": "evidence.receipt_fact", "terminal": False},
        {"seq": 2, "kind": "artifact.published", "terminal": True},
        {"seq": 3, "kind": "evidence.receipt_fact", "terminal": False},
    ]

    frames = [
        frame
        async for frame in journal_stream(
            lambda after: [r for r in rows if r["seq"] > after], poll_s=0.01
        )
    ]

    assert [f.splitlines()[0] for f in frames] == ["id: 1", "id: 2"]


@pytest.mark.asyncio
async def test_journal_stream_resumes_strictly_after_the_cursor() -> None:
    rows = [{"seq": n, "terminal": n == 3} for n in (1, 2, 3)]

    frames = [
        frame
        async for frame in journal_stream(
            lambda after: [r for r in rows if r["seq"] > after],
            after_seq=1,
            poll_s=0.01,
        )
    ]

    assert [f.splitlines()[0] for f in frames] == ["id: 2", "id: 3"]


# --------------------------------------------------------------------------- #
# I17 Task 69 — authorization, caching, and restart, exercised end to end
# --------------------------------------------------------------------------- #


class TestTheProtectedPromptSessionMustStillBeReal:
    def test_an_expired_session_cannot_read_the_prompt(
        self, tmp_path: Path, inbox_client
    ):
        clock = {"t": 0.0}
        sessions = SessionStore(ttl_seconds=100.0, now=lambda: clock["t"])
        app = _mk_app(tmp_path, expose=False, sessions=sessions)
        sid, _ = sessions.create()
        client = inbox_client(app, loopback_peer=True)
        client.cookies.set("kew_session", sid)

        clock["t"] = 101.0  # the 12h TTL is not sliding: it simply runs out

        assert client.get("/api/runs/r1/prompt").status_code == 401

    def test_a_server_restart_revokes_the_prompt_session(
        self, tmp_path: Path, inbox_client
    ):
        app = _mk_app(tmp_path, expose=False)
        sid, _ = app.state.sessions.create()

        # Sessions live in-process, so a restart IS revocation: the same cookie
        # against the restarted app is a cookie for a session that never was.
        restarted = _mk_app(tmp_path, expose=False)
        client = inbox_client(restarted, loopback_peer=True)
        client.cookies.set("kew_session", sid)

        assert client.get("/api/runs/r1/prompt").status_code == 401


class TestTheCanonicalReadsStayAvailableWhereThePromptDoesNot:
    def test_an_exposed_session_still_reads_the_evidence_projection(
        self, tmp_path: Path, inbox_client
    ):
        app = _mk_app(tmp_path, expose=True)
        client = _session_client(app, inbox_client)

        assert client.get("/api/runs/r1/flight").status_code == 200
        assert client.get("/api/runs/r1/prompt").status_code == 403


class TestBrowserLikeCaching:
    def test_the_prompt_response_is_uncacheable_and_unrevalidatable(
        self, tmp_path: Path, inbox_client
    ):
        app = _mk_app(tmp_path, expose=False)
        client = _session_client(app, inbox_client)

        r = client.get("/api/runs/r1/prompt")

        assert "no-store" in r.headers["cache-control"]
        assert "private" in r.headers["cache-control"]
        assert r.headers.get("pragma") == "no-cache"
        # No revalidation handle: a validator would let a cache hold the bytes
        # and re-serve them on a 304 without the origin ever being consulted.
        assert "etag" not in r.headers
        assert "last-modified" not in r.headers

    def test_ordinary_reads_are_not_marked_no_store(self, tmp_path: Path, inbox_client):
        """The header means something only if it is not on everything."""
        c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)

        assert "no-store" not in c.get("/api/runs").headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_journal_stream_does_not_spin_on_a_cursor_that_never_moves() -> None:
    """Re-served rows are not progress: the idle timer must still run out."""
    polls = {"n": 0}

    def _stuck(after_seq: int) -> list[dict]:
        polls["n"] += 1
        return [{"seq": after_seq, "terminal": False}]  # never advances

    frames = [
        frame
        async for frame in journal_stream(
            _stuck, after_seq=7, poll_s=0.01, max_seconds=0.03
        )
    ]

    assert polls["n"] <= 6  # bounded by max_seconds/poll_s, not unbounded
    assert len(frames) == polls["n"]
