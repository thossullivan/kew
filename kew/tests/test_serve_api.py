"""Tests for the kew serve FastAPI app (REST + SSE)."""

import json
import logging
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kew.audit import Actor
from kew.evidence import EvidenceKind, Producer, ProducerKind, record_evidence
from kew.run_store import RunRecord, SqliteRunStore
from kew.sensitive_prompt import (
    PROMPT_ARTIFACT_KIND,
    RETENTION_DAYS,
    default_root,
    prompt_digest,
    purge_expired_prompts,
    retain_prompt,
)
from kew.serve import api_runs as api_runs_module
from kew.serve.app import create_app
from kew.serve.auth import SessionStore


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


@pytest.fixture
def client(tmp_path: Path, inbox_client) -> TestClient:
    (tmp_path / "r1.log").write_text("log\n")
    (tmp_path / "r1.events.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"ts": 1.0, "type": "started", "issue": 312},
                {"ts": 2.0, "type": "tool_use", "tool": "edit", "target": "a.py"},
                {"ts": 3.0, "type": "finished", "exit_code": 0},
            ]
        )
        + "\n"
    )
    store = FakeStore([FakeRecord("r1", tmp_path / "r1.log")])
    app = create_app(lambda: nullcontext(store), repo_dir=tmp_path)
    return inbox_client(app, loopback_peer=True)


def test_list_runs(client: TestClient):
    r = client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["run_id"] == "r1" and body[0]["issue_num"] == 312


def test_get_timeline(client: TestClient):
    r = client.get("/api/runs/r1/timeline")
    assert r.status_code == 200
    assert [n["kind"] for n in r.json()["nodes"]] == ["init", "edit", "result"]


def test_unknown_run_404(client: TestClient):
    assert client.get("/api/runs/nope/timeline").status_code == 404


def test_events_stream_is_sse(client: TestClient):
    with client.stream("GET", "/api/runs/r1/events?replay=true") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        first = next(r.iter_lines())
        assert first.startswith("data:")


def test_run_has_channel(client: TestClient):
    # The fake record uses a hosted model -> hosted channel.
    assert client.get("/api/runs").json()[0]["channel"] == "hosted"


def test_run_detail_has_honest_missing_posture(client: TestClient) -> None:
    body = client.get("/api/runs/r1").json()

    assert body.get("posture") == "not recorded"


def test_run_detail_carries_recorded_posture_fields(
    tmp_path: Path, inbox_client
) -> None:
    class EventStore(FakeStore):
        def list_audit_events(self, **_kwargs):
            event = type(
                "Event",
                (),
                {
                    "run_id": "r1",
                    "payload": {
                        "posture": "sandboxed",
                        "filesystem_boundary": "container-mount",
                        "network_boundary": "sidecar-certified",
                        "credential_names": ["ANTHROPIC_API_KEY"],
                        "command_guard": "loaded",
                        "admission": "policy",
                        "reason": None,
                    },
                },
            )
            return [event]

    store = EventStore([FakeRecord("r1", tmp_path / "r1.log")])
    app = create_app(lambda: nullcontext(store), repo_dir=tmp_path)
    body = inbox_client(app, loopback_peer=True).get("/api/runs/r1").json()

    assert body["posture"] == "sandboxed"
    assert body["filesystem_boundary"] == "container-mount"
    assert body["network_boundary"] == "sidecar-certified"
    assert body["credential_names"] == ["ANTHROPIC_API_KEY"]
    assert body["command_guard"] == "loaded"
    assert body["admission"] == "policy"
    assert body["reason"] is None


# --------------------------------------------------------------------------- #
# FUI-13 — the stage a live run is executing
#
# Governed review IS a row in the runs table (`adversarial_review` for the
# reviewer, `review_remediation` for the driver-invoked remediation pass), so
# these read the stage straight off /api/runs. The Monitor needs no second
# source, and nothing here is composed from a status word alone.
# --------------------------------------------------------------------------- #

_PR = "https://github.com/thossullivan/kew/pull/2059"


def _run(run_id: str, **over) -> RunRecord:
    fields = {
        "run_id": run_id,
        "issue_num": 2055,
        "item_id": "2055",
        "branch": "agent/issue-2055",
        "status": "pending",
        "exit_code": 0,
        "started_at": _STAMP,
        "runner_name": "claude-code",
        "model": "claude-opus-4-8",
        "title": "review gate lows",
    }
    return RunRecord(**{**fields, **over})


def _authoring_run(run_id: str = "d1") -> RunRecord:
    """The dispatch run that opened the PR, which is the only row carrying it.

    ``dispatch_core._persist_pr_url`` stamps this row when the PR is created.
    Nothing ever stamps a review / ci_fix / resolve row (they work on a PR they
    did not open), so these tests must never fabricate a ``pr_url`` on one —
    doing so would assert a fact no writer produces.
    """
    return _run(run_id, status="success", finished_at=_STAMP, pr_url=_PR)


@pytest.fixture
def stages(tmp_path: Path, inbox_client):
    """Seed runs (and finished review rounds) into a real store, read stages back."""

    def read(records, rounds: tuple[str, ...] = ()) -> dict[str, tuple[str, str]]:
        db = tmp_path / "dispatch.db"
        with SqliteRunStore(str(db)) as store:
            for record in records:
                store.save(record)
            for verdict in rounds:
                store.record_review_round(
                    "factory-ui", "2055", head_sha="3670e43f", verdict=verdict
                )
        app = create_app(lambda: SqliteRunStore(str(db)), repo_dir=tmp_path)
        body = inbox_client(app, loopback_peer=True).get("/api/runs").json()
        return {row["run_id"]: (row["stage"], row["stage_detail"]) for row in body}

    return read


def test_an_in_flight_dispatch_run_is_implementing(stages) -> None:
    read = stages([_run("r1"), _run("r2", attempt_number=2)])

    assert read["r1"] == ("implement", "")
    # A retry says which attempt it is; the number is on the row, not inferred.
    assert read["r2"] == ("implement", "attempt 2")


def test_a_live_reviewer_names_the_round_it_is_reviewing(stages) -> None:
    # A round row is written when the round ENDS, so the live reviewer's own
    # round is one past the rounds already recorded in this cycle.
    read = stages(
        [_authoring_run(), _run("r1", run_kind="adversarial_review")],
        rounds=("NEEDS_CHANGES",),
    )

    assert read["r1"] == ("review", "round 2 · PR #2059")


def test_the_first_reviewer_of_a_cycle_is_round_one(stages) -> None:
    read = stages([_authoring_run(), _run("r1", run_kind="adversarial_review")])

    assert read["r1"] == ("review", "round 1 · PR #2059")


def test_a_live_reviewer_reads_its_pr_off_the_run_that_opened_it(stages) -> None:
    """The reviewer's own row carries no PR, and the detail line still has one.

    Regression for the gap under the acceptance criteria: every writer of
    ``pr_url`` stamps the run that OPENED the pull request, so a governed
    reviewer's row is empty by construction and reading only that row could
    never produce the "round 1 · PR #2059" the artboard shows. The PR is
    recovered by the store's own branch join instead.
    """
    reviewer = _run("r1", run_kind="adversarial_review")
    assert reviewer.pr_url is None

    read = stages([_authoring_run(), reviewer])

    assert read["r1"] == ("review", "round 1 · PR #2059")


def test_a_reviewer_on_a_branch_with_no_recorded_pr_omits_it(stages) -> None:
    # Nothing on this branch ever opened a PR, so there is no PR to name and
    # the detail line carries the round alone rather than inventing a number.
    read = stages([_run("r1", run_kind="adversarial_review")])

    assert read["r1"] == ("review", "round 1")


def test_a_live_remediation_belongs_to_the_round_it_is_fixing(stages) -> None:
    # Remediation is dispatched to fix what round 1 found, so it is round 1 —
    # not the round 2 that has not been asked for yet.
    read = stages(
        [_authoring_run(), _run("r1", run_kind="review_remediation")],
        rounds=("NEEDS_CHANGES",),
    )

    assert read["r1"] == ("review", "round 1 · PR #2059")


def test_a_finished_review_run_claims_no_round(stages) -> None:
    # review_rounds rows carry no run id, so which round a past run served
    # cannot be recovered. The payload says nothing rather than guessing.
    read = stages(
        [
            _authoring_run(),
            _run(
                "r1",
                run_kind="adversarial_review",
                status="success",
                finished_at=_STAMP,
            ),
        ],
        rounds=("READY",),
    )

    assert read["r1"] == ("review", "PR #2059")


def test_awaiting_publication_has_reached_the_publish_stage(stages) -> None:
    # The compatibility encoding of an implementation run that DID succeed with
    # only publication outstanding.
    read = stages([_run("r1", status="awaiting_publication")])

    assert read["r1"] == ("publish", "")


def test_a_ci_fix_is_its_own_stage(stages) -> None:
    # A ci_fix works on an already-open PR it did not open, so its PR comes
    # from the same branch join the reviewer's does.
    read = stages([_authoring_run(), _run("r1", run_kind="ci_fix")])

    assert read["r1"] == ("ci_fix", "PR #2059")


@pytest.mark.parametrize(
    "run_kind",
    [
        # A conflict resolution on an open PR, and a readiness probe that
        # carries no work item: kew's vocabulary has no word for either.
        "resolve",
        "readiness_probe",
        # `kew review <PR>` on a PR with no tracked item is UNgoverned, so
        # calling it "review" would let the Monitor caption it governed.
        "review",
        # A kind from some newer host.
        "backport",
    ],
)
def test_an_unmappable_run_kind_is_unknown_and_names_itself(stages, run_kind) -> None:
    read = stages([_run("r1", run_kind=run_kind)])

    assert read["r1"] == ("unknown", run_kind)


def test_an_unnamed_stage_still_names_the_pr_it_is_working_on(stages) -> None:
    # `resolve` has no stage word, but it IS a PR-subject kind, so the branch
    # join still recovers what it is resolving. The kind comes first: an
    # operator needs the word before the reference it applies to.
    read = stages([_authoring_run(), _run("r1", run_kind="resolve")])

    assert read["r1"] == ("unknown", "resolve · PR #2059")


def test_a_store_without_the_run_kind_column_is_unknown_not_implement(
    client: TestClient,
) -> None:
    # A narrow read store (or a row predating the column) records no kind.
    # Reading that as "implement" would put a confident stage on a guess.
    assert client.get("/api/runs").json()[0]["stage"] == "unknown"


def test_a_store_that_cannot_count_rounds_omits_the_round_and_says_why(
    tmp_path: Path, inbox_client, caplog: pytest.LogCaptureFixture
) -> None:
    class Unreadable(FakeStore):
        def review_round_count(self, item_id: str) -> int:
            raise RuntimeError("review_rounds is unreadable")

    record = FakeRecord("r1", tmp_path / "r1.log")
    record.run_kind = "adversarial_review"
    record.status = "pending"
    record.item_id = "2055"
    app = create_app(lambda: nullcontext(Unreadable([record])), repo_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        body = inbox_client(app, loopback_peer=True).get("/api/runs").json()

    assert (body[0]["stage"], body[0]["stage_detail"]) == ("review", "")
    assert "review round count unreadable for item 2055" in caplog.text


def test_a_store_whose_branch_join_fails_omits_the_pr_and_says_why(
    tmp_path: Path, inbox_client, caplog: pytest.LogCaptureFixture
) -> None:
    class Unjoinable(FakeStore):
        def review_round_count(self, item_id: str) -> int:
            return 0

        def find_run_by_branch(self, branch: str):
            raise RuntimeError("runs is unreadable by branch")

    record = FakeRecord("r1", tmp_path / "r1.log")
    record.run_kind = "adversarial_review"
    record.status = "pending"
    record.item_id = "2055"
    record.branch = "agent/issue-2055"
    app = create_app(lambda: nullcontext(Unjoinable([record])), repo_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        body = inbox_client(app, loopback_peer=True).get("/api/runs").json()

    # The round survives — the two reads fail independently.
    assert (body[0]["stage"], body[0]["stage_detail"]) == ("review", "round 1")
    assert "branch join unreadable for agent/issue-2055" in caplog.text


def test_meta_defaults_to_metered(client: TestClient):
    body = client.get("/api/meta").json()
    assert body["billing"] == "metered"
    assert body["plan_monthly_usd"] == 0.0


def test_meta_reflects_injected_billing(tmp_path: Path, inbox_client):
    from kew.service.models import MetaDTO

    store = FakeStore([FakeRecord("r1", tmp_path / "r1.log")])
    app = create_app(
        lambda: nullcontext(store),
        repo_dir=tmp_path,
        meta=MetaDTO(
            billing="subscription",
            plan_label="Claude Max",
            plan_monthly_usd=200.0,
            version="1.2.3",
        ),
    )
    body = inbox_client(app, loopback_peer=True).get("/api/meta").json()
    assert body["billing"] == "subscription"
    assert body["plan_label"] == "Claude Max"
    assert body["plan_monthly_usd"] == 200.0


# --------------------------------------------------------------------------- #
# I17 — canonical evidence reads and the one protected prompt response
# --------------------------------------------------------------------------- #

_PROMPT = "SYSTEM: compiled dispatch prompt\nSECRET-PROMPT-BYTES-do-not-leak\n"
_ACTOR = Actor("human", "tester")
_HOST = Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="1.2.3")
_STAMP = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _seed_evidence(db: Path, project_root: Path) -> list[int]:
    """One run, a three-row journal, one retained prompt — as dispatch writes them."""
    with SqliteRunStore(str(db)) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=312,
                branch="agent/issue-312",
                status="success",
                exit_code=0,
                started_at=_STAMP,
                finished_at=_STAMP,
                runner_name="claude-code",
                model="claude-opus-4-8",
                title="rate-limit headers",
            )
        )
        seqs = [
            record_evidence(
                store,
                EvidenceKind.RECEIPT_FACT,
                actor=_ACTOR,
                producer=_HOST,
                run_id="run-1",
                facts={"fact": "outcome", "value": value},
            )
            for value in ("one", "two", "three")
        ]
        ref = retain_prompt(
            default_root(project_root),
            run_id="run-1",
            attempt_number=1,
            text=_PROMPT,
            now=_STAMP,
        )
        store.record_artifact(
            run_id="run-1",
            attempt_number=1,
            kind=PROMPT_ARTIFACT_KIND,
            digest=ref.digest,
            byte_length=ref.byte_length,
            locator=ref.locator,
            created_at=_STAMP,
        )
    return seqs


@pytest.fixture
def evidence(tmp_path: Path):
    """``(app, seqs)`` for a serve app over a real evidence-bearing store."""
    db = tmp_path / "dispatch.db"
    seqs = _seed_evidence(db, tmp_path)
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        sessions=SessionStore(),
    )
    return app, seqs


def _authenticated(app, inbox_client):
    """A loopback client carrying a real session cookie."""
    sid, _ = app.state.sessions.create()
    client = inbox_client(app, loopback_peer=True)
    client.cookies.set("kew_session", sid)
    return client


def _bound_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the SSE loop: TestClient drives the ASGI call to completion, so an
    unbounded stream can never be drained inside a test (see api_epics tests)."""
    real = api_runs_module.journal_stream

    def _bounded(fetch, **kwargs):
        return real(fetch, **{**kwargs, "poll_s": 0.01, "max_seconds": 0.05})

    monkeypatch.setattr(api_runs_module, "journal_stream", _bounded)


def _frames(text: str) -> list[dict]:
    return [
        json.loads(line[len("data:") :].strip())
        for line in text.splitlines()
        if line.startswith("data:")
    ]


class TestTheCanonicalEvidenceRead:
    def test_the_flight_route_projects_the_host_sequence(self, evidence, inbox_client):
        app, seqs = evidence
        body = (
            inbox_client(app, loopback_peer=True).get("/api/runs/run-1/flight").json()
        )

        assert [event["seq"] for event in body["events"]] == seqs
        assert body["run_id"] == "run-1"
        assert body["chain_verified"] is True
        assert body["sealed"] is False
        assert {row["dimension"] for row in body["availability"]}

    def test_the_flight_route_404s_an_unknown_run(self, evidence, inbox_client):
        app, _ = evidence
        client = inbox_client(app, loopback_peer=True)
        assert client.get("/api/runs/nope/flight").status_code == 404


class TestTheJournalStream:
    def test_it_tails_the_journal_as_sse(self, evidence, inbox_client, monkeypatch):
        _bound_journal(monkeypatch)
        app, seqs = evidence
        r = inbox_client(app, loopback_peer=True).get("/api/runs/run-1/journal")

        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert [frame["seq"] for frame in _frames(r.text)] == seqs

    def test_every_frame_carries_its_sequence_as_the_sse_id(
        self, evidence, inbox_client, monkeypatch
    ):
        _bound_journal(monkeypatch)
        app, seqs = evidence
        r = inbox_client(app, loopback_peer=True).get("/api/runs/run-1/journal")

        ids = [line for line in r.text.splitlines() if line.startswith("id:")]
        assert ids == [f"id: {seq}" for seq in seqs]

    def test_a_reconnect_resumes_after_the_last_event_id(
        self, evidence, inbox_client, monkeypatch
    ):
        _bound_journal(monkeypatch)
        app, seqs = evidence
        r = inbox_client(app, loopback_peer=True).get(
            "/api/runs/run-1/journal", headers={"last-event-id": str(seqs[0])}
        )

        assert [frame["seq"] for frame in _frames(r.text)] == seqs[1:]

    def test_a_restarted_server_resumes_from_the_same_durable_cursor(
        self, tmp_path: Path, inbox_client, monkeypatch
    ):
        _bound_journal(monkeypatch)
        db = tmp_path / "dispatch.db"
        seqs = _seed_evidence(db, tmp_path)
        # A fresh app is a restart: new process state, same durable ledger.
        restarted = create_app(
            lambda: SqliteRunStore(str(db)),
            repo_dir=tmp_path,
            sessions=SessionStore(),
        )
        r = inbox_client(restarted, loopback_peer=True).get(
            "/api/runs/run-1/journal", headers={"last-event-id": str(seqs[1])}
        )

        assert [frame["seq"] for frame in _frames(r.text)] == seqs[2:]

    def test_the_journal_route_404s_an_unknown_run(
        self, evidence, inbox_client, monkeypatch
    ):
        _bound_journal(monkeypatch)
        app, _ = evidence
        client = inbox_client(app, loopback_peer=True)
        assert client.get("/api/runs/nope/journal").status_code == 404


class TestTheProtectedPromptResponse:
    def test_a_loopback_read_without_a_session_is_refused(self, evidence, inbox_client):
        app, _ = evidence
        # read_guard lets loopback read the ordinary projections; the prompt is
        # not an ordinary projection, so the exemption does not extend to it.
        assert (
            inbox_client(app, loopback_peer=True).get("/api/runs/run-1").status_code
            == 200
        )
        assert (
            inbox_client(app, loopback_peer=True)
            .get("/api/runs/run-1/prompt")
            .status_code
            == 401
        )

    def test_a_real_session_gets_the_exact_bytes(self, evidence, inbox_client):
        app, _ = evidence
        r = _authenticated(app, inbox_client).get("/api/runs/run-1/prompt")

        assert r.status_code == 200
        body = r.json()
        assert body["text"] == _PROMPT
        assert body["availability"] == "available"
        assert body["digest"] == prompt_digest(_PROMPT)
        assert body["byte_length"] == len(_PROMPT.encode())

    def test_the_response_is_private_and_never_stored(self, evidence, inbox_client):
        app, _ = evidence
        r = _authenticated(app, inbox_client).get("/api/runs/run-1/prompt")

        cache_control = r.headers["cache-control"]
        assert "no-store" in cache_control
        assert "private" in cache_control

    def test_a_run_that_retained_nothing_is_a_404(self, evidence, inbox_client):
        app, _ = evidence
        client = _authenticated(app, inbox_client)
        assert client.get("/api/runs/nope/prompt").status_code == 404

    def test_an_expired_prompt_keeps_its_digest_and_omits_the_bytes(
        self, tmp_path: Path, inbox_client
    ):
        db = tmp_path / "dispatch.db"
        _seed_evidence(db, tmp_path)
        with SqliteRunStore(str(db)) as store:
            store.set_artifact_retention(
                run_id="run-1",
                attempt_number=1,
                kind=PROMPT_ARTIFACT_KIND,
                terminal_at=_STAMP,
            )
            purge_expired_prompts(
                store,
                default_root(tmp_path),
                now=_STAMP + timedelta(days=RETENTION_DAYS + 1),
            )
        app = create_app(
            lambda: SqliteRunStore(str(db)), repo_dir=tmp_path, sessions=SessionStore()
        )

        body = _authenticated(app, inbox_client).get("/api/runs/run-1/prompt").json()

        assert body["text"] is None
        assert body["availability"] == "unavailable"
        assert body["digest"] == prompt_digest(_PROMPT)
        assert body["purged_at"] and body["purge_reason"] == "retention"


class TestTheBytesReachNoOtherPayload:
    def test_no_other_read_carries_the_prompt(
        self, evidence, inbox_client, monkeypatch
    ):
        _bound_journal(monkeypatch)
        app, seqs = evidence
        client = _authenticated(app, inbox_client)

        for path in (
            "/api/runs",
            "/api/runs/run-1",
            "/api/runs/run-1/timeline",
            "/api/runs/run-1/flight",
            f"/api/runs/run-1/nodes/run-1:{seqs[-1]}/diff",
            "/api/runs/run-1/journal",
        ):
            assert "SECRET-PROMPT-BYTES" not in client.get(path).text, path

        assert "SECRET-PROMPT-BYTES" in client.get("/api/runs/run-1/prompt").text

    def test_the_openapi_schema_carries_no_prompt_example(self, evidence):
        app, _ = evidence
        assert "SECRET-PROMPT-BYTES" not in json.dumps(app.openapi())

    def test_the_protected_read_writes_no_bytes_to_the_log(
        self, evidence, inbox_client, caplog
    ):
        app, _ = evidence
        with caplog.at_level(logging.DEBUG):
            assert (
                _authenticated(app, inbox_client)
                .get("/api/runs/run-1/prompt")
                .status_code
                == 200
            )

        assert "SECRET-PROMPT-BYTES" not in caplog.text


class TestOnlyOneRouteCanEverAnswerWithThePrompt:
    def test_exactly_one_path_references_the_protected_schema(self, evidence):
        """A structural check, not a sampled one.

        The byte-absence tests above prove the reads we exercised carry no
        prompt. This proves the stronger thing: no OTHER route in the whole
        API declares a response that could carry one, so a future read cannot
        acquire the capability by accident.
        """
        app, _ = evidence
        schema = app.openapi()

        carriers = sorted(
            path
            for path, methods in schema["paths"].items()
            if "ProtectedPromptDTO" in json.dumps(methods)
        )

        assert carriers == ["/api/runs/{run_id}/prompt"]

    def test_no_schema_declares_a_second_prompt_bearing_field(self, evidence):
        app, _ = evidence
        schemas = app.openapi()["components"]["schemas"]

        bearing = {
            name
            for name, model in schemas.items()
            if {"prompt", "prompt_text", "compiled_prompt"}
            & set(model.get("properties", {}))
        }

        assert bearing == set()

    def test_the_evidence_journal_refuses_prompt_facts_at_the_writer(self, evidence):
        """Defence in depth: the ledger cannot hold the bytes to leak."""
        from kew.evidence import RESERVED_FACT_KEYS

        assert {"prompt", "prompt_text", "compiled_prompt"} <= RESERVED_FACT_KEYS


class TestTheLogoutRouteIsGone:
    """A07/X5: `POST /api/auth/logout` had no caller in any shipped client.

    The guarantee it provided — an explicit, browser-initiated session
    revocation — now lives nowhere. A session ends when its 12h TTL expires or
    when the server restarts, because the session store is in-process. Login
    and the session probe stay: the inbox page calls both.
    """

    def test_logout_is_not_routed_even_for_a_session_holder(
        self, evidence, inbox_client
    ):
        """405, not 404: the SPA catch-all (`app.py`) matches every path for
        GET, so an unrouted POST is a method mismatch rather than an unknown
        path. `POST /api/auth/login` on the same client proves the assertion
        discriminates — a live write route answers, a deleted one does not.
        """
        app, _ = evidence
        sid, csrf = app.state.sessions.create()
        client = inbox_client(app, loopback_peer=True)
        client.cookies.set("kew_session", sid)
        headers = {"X-Kew-CSRF": csrf}

        assert client.post("/api/auth/logout", headers=headers).status_code == 405
        assert (
            client.post(
                "/api/auth/login", json={"token": "nope"}, headers=headers
            ).status_code
            == 401
        )

    def test_the_published_auth_surface_is_login_and_session_only(self, evidence):
        app, _ = evidence

        auth_paths = sorted(
            p for p in app.openapi()["paths"] if p.startswith("/api/auth")
        )

        assert auth_paths == ["/api/auth/login", "/api/auth/session"]
