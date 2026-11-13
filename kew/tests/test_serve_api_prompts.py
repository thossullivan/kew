"""``GET /api/prompts`` serves the read-only prompt catalog (#3018)."""

from __future__ import annotations

import hashlib
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from support.reviewer_artifact import commit_reviewer_artifact, git_repo, lose_blob
from typer.testing import CliRunner

from kew.audit import Actor
from kew.cli import app as cli_app
from kew.config import DispatchConfig
from kew.prompt_previews import PREVIEW_RENDERERS
from kew.run_store import RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.service import prompt_profiles as profiles
from kew.service.prompts import ROLES, SAMPLE_TITLE

_REAL_TITLES = ("Rotate the vault token nightly", "Fix flaky checkout retry")
_ARTIFACT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kew"
    / "_skills"
    / "adversarial-reviewer"
    / "PROMPT.md"
)


@contextmanager
def _store_factory(db_path: str):
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


def _app(tmp_path: Path, *, expose: bool = False, renderers=PREVIEW_RENDERERS):
    db = str(tmp_path / "kew.db")
    store = SqliteRunStore(db)
    for n, title in enumerate(_REAL_TITLES, start=40):
        store.save(
            RunRecord(
                run_id=f"r{n}",
                issue_num=n,
                branch=f"agent/issue-{n}",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 10, 1, tzinfo=UTC),
                title=title,
            )
        )
    store.close()
    return create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        expose=expose,
        prompt_renderers=renderers,
    )


@pytest.fixture
def client(tmp_path: Path, inbox_client):
    return inbox_client(_app(tmp_path), loopback_peer=True)


def test_the_list_names_every_role_with_its_source(client) -> None:
    response = client.get("/api/prompts")
    assert response.status_code == 200
    rows = {row["role"]: row for row in response.json()}
    assert list(rows) == [entry.role for entry in ROLES]
    for entry in ROLES:
        assert rows[entry.role]["source"]["kind"] == entry.source_kind
    reviewer = rows["adversarial_review"]["source"]
    assert reviewer["name"] == "adversarial-reviewer"
    assert isinstance(reviewer["version"], int)
    assert len(reviewer["sha256"]) == 64


@pytest.mark.parametrize(
    ("template", "kind", "name"),
    [
        ("", "packaged-template", "dispatch.md.j2"),
        ("prompts/custom.md.j2", "config-template", "prompts/custom.md.j2"),
    ],
)
def test_the_dispatch_source_is_the_template_in_effect(
    tmp_path: Path, inbox_client, template: str, kind: str, name: str
) -> None:
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    config = DispatchConfig()
    config.prompt.template = template
    app = create_app(
        lambda: _store_factory(db),
        config=config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    c = inbox_client(app, loopback_peer=True)
    rows = {row["role"]: row for row in c.get("/api/prompts").json()}
    assert rows["dispatch"]["source"]["kind"] == kind
    assert rows["dispatch"]["source"]["name"] == name
    preview = c.get("/api/prompts/dispatch").json()
    assert preview["source"] == rows["dispatch"]["source"]


def test_the_reviewer_preview_carries_the_artifact_and_its_own_sha(client) -> None:
    response = client.get("/api/prompts/adversarial_review")
    assert response.status_code == 200
    body = response.json()
    assert _ARTIFACT.read_text(encoding="utf-8") in body["preview"]
    assert (
        body["preview_sha256"]
        == hashlib.sha256(body["preview"].encode("utf-8")).hexdigest()
    )
    assert body["source"]["kind"] == "pinned-artifact"


def test_a_preview_carries_the_sample_item_and_no_title_from_the_store(
    client,
) -> None:
    assert SAMPLE_TITLE in client.get("/api/prompts/dispatch").json()["preview"]
    for entry in ROLES:
        preview = client.get(f"/api/prompts/{entry.role}").json()["preview"]
        for title in _REAL_TITLES:
            assert title not in preview, (entry.role, title)


def test_an_unknown_role_is_a_404_with_a_sentence(client) -> None:
    response = client.get("/api/prompts/no_such_role")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "no_such_role" in detail
    assert detail.endswith(".")


@pytest.mark.parametrize("path", ["/api/prompts", "/api/prompts/dispatch"])
def test_an_exposed_read_without_a_session_is_refused(
    tmp_path: Path, inbox_client, path: str
) -> None:
    exposed = inbox_client(_app(tmp_path, expose=True), loopback_peer=True)
    assert exposed.get(path).status_code == 401


def test_a_server_started_without_renderers_answers_503_with_a_sentence(
    tmp_path: Path, inbox_client
) -> None:
    bare = inbox_client(_app(tmp_path, renderers=None), loopback_peer=True)
    assert bare.get("/api/prompts").status_code == 200
    response = bare.get("/api/prompts/dispatch")
    assert response.status_code == 503
    assert response.json()["detail"].endswith(".")


def test_kew_serve_wires_the_preview_renderers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client
) -> None:
    monkeypatch.chdir(tmp_path)
    served: list = []
    with (
        patch("kew.cli.preflight_tools"),
        patch("uvicorn.run", side_effect=lambda app, **_: served.append(app)),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        result = CliRunner().invoke(cli_app, ["serve"])
    assert result.exit_code == 0, result.output
    response = inbox_client(served[0], loopback_peer=True).get("/api/prompts/dispatch")
    assert response.status_code == 200
    assert SAMPLE_TITLE in response.json()["preview"]


def test_the_served_projects_deployed_reviewer_copy_wins(
    tmp_path: Path, inbox_client
) -> None:
    deployed = "# Deployed reviewer rubric\n\nReview with care.\n"
    commit_reviewer_artifact(git_repo(tmp_path), deployed)
    served = inbox_client(_app(tmp_path), loopback_peer=True)
    listed = {row["role"]: row for row in served.get("/api/prompts").json()}
    source = listed["adversarial_review"]["source"]
    assert source["kind"] == "deployed-artifact"
    assert source["sha256"] == hashlib.sha256(deployed.encode()).hexdigest()
    body = served.get("/api/prompts/adversarial_review").json()
    assert body["preview"].startswith(deployed)
    assert body["source"] == source


def test_the_dispatch_preview_says_which_variant_it_shows(client) -> None:
    variant = client.get("/api/prompts/dispatch").json()["variant"]
    assert variant["contained"] is True
    assert variant["publication"] == "host"


def test_the_reviewer_source_sha_is_the_previews_artifact_across_a_redeploy(
    tmp_path: Path, inbox_client
) -> None:
    git_repo(tmp_path)
    served = inbox_client(_app(tmp_path), loopback_peer=True)
    for deployed in ("# First reviewer copy\n", "# Second reviewer copy\n"):
        commit_reviewer_artifact(tmp_path, deployed)
        body = served.get("/api/prompts/adversarial_review").json()
        assert body["preview"].startswith(deployed)
        assert body["source"]["sha256"] == hashlib.sha256(deployed.encode()).hexdigest()


def test_a_deployed_reviewer_copy_git_cannot_read_answers_503_not_500(
    tmp_path: Path, inbox_client
) -> None:
    """The base ref lists a copy whose object is gone: the prompt the review
    would run is unknown, so neither route serves a preview of another."""
    deployed = "# A reviewer rubric whose object was lost\n"
    commit_reviewer_artifact(git_repo(tmp_path), deployed)
    lose_blob(tmp_path, deployed)
    served = inbox_client(_app(tmp_path), loopback_peer=True)

    listed = served.get("/api/prompts")
    body = served.get("/api/prompts/adversarial_review")

    for response in (listed, body):
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert "adversarial_review" in detail
        assert ".claude/kew/adversarial-reviewer.md" in detail


# --- Operator profile writes (#3025) -----------------------------------------

_BODY = "Keep every change minimal and name the file you touched."


def _write_app(tmp_path: Path, approvers: list[str]):
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    config = DispatchConfig()
    config.approval.approvers = approvers
    return create_app(
        lambda: _store_factory(db),
        config=config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )


def _login(c) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert r.status_code == 200
    return {"X-Kew-CSRF": r.json()["csrf_token"]}


@pytest.fixture
def writer(tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch):
    """A logged-in client acting as ``alice``, the one approver."""
    monkeypatch.setenv("KEW_ACTOR", "alice")
    c = inbox_client(_write_app(tmp_path, ["alice"]), loopback_peer=True)
    c.headers.update(_login(c))
    return c


def _propose(c, body: str = _BODY) -> dict:
    r = c.post("/api/prompts/dispatch/versions", json={"body": body})
    assert r.status_code == 200, r.text
    return r.json()


#: Each profile write and the JSON body it takes.
_WRITES = (
    ("/api/prompts/dispatch/versions", {"body": _BODY}),
    ("/api/prompts/dispatch/versions/1/approve", {"generation": 0}),
    ("/api/prompts/dispatch/versions/1/decline", {}),
    ("/api/prompts/dispatch/retire", {"generation": 0}),
)


@pytest.mark.parametrize(("path", "body"), _WRITES)
def test_a_profile_write_without_a_session_is_refused(
    tmp_path: Path, inbox_client, path: str, body: dict
) -> None:
    c = inbox_client(_write_app(tmp_path, []), loopback_peer=True)
    assert c.post(path, json=body).status_code == 401
    assert c.get("/api/prompts/dispatch/versions").json()["versions"] == []


@pytest.mark.parametrize(("path", "body"), _WRITES)
def test_a_profile_write_without_the_csrf_header_is_refused(
    tmp_path: Path, inbox_client, path: str, body: dict
) -> None:
    c = inbox_client(_write_app(tmp_path, []), loopback_peer=True)
    _login(c)
    assert c.post(path, json=body).status_code == 403
    assert c.get("/api/prompts/dispatch/versions").json()["versions"] == []


def test_propose_then_approve_makes_v1_active_and_previews_it(writer) -> None:
    proposed = _propose(writer)
    assert proposed["active_version"] is None
    assert [(v["version"], v["state"]) for v in proposed["versions"]] == [
        (1, "proposed")
    ]
    assert proposed["max_body_chars"] == 4000

    r = writer.post(
        "/api/prompts/dispatch/versions/1/approve",
        json={"generation": proposed["generation"]},
    )
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["active_version"] == 1
    v1 = approved["versions"][0]
    assert (v1["state"], v1["decided_by"]) == ("active", "alice")
    assert isinstance(v1["audit_event_id"], int)

    preview = writer.get("/api/prompts/dispatch").json()
    assert "## Operator instructions — dispatch profile v1" in preview["preview"]
    assert _BODY in preview["preview"]


def test_the_preview_names_the_generation_its_history_read_serves(writer) -> None:
    """The preview and the history each say which generation they show, so
    the console can refuse to pair two decisions' views."""

    def generations() -> tuple:
        preview = writer.get("/api/prompts/dispatch").json()
        history = writer.get("/api/prompts/dispatch/versions").json()
        return preview["profile_generation"], history["generation"]

    assert generations() == (0, 0)
    proposed = _propose(writer)
    writer.post(
        "/api/prompts/dispatch/versions/1/approve",
        json={"generation": proposed["generation"]},
    )
    assert generations() == (1, 1)
    applies = {e.role for e in ROLES if e.profile_applies}
    other = next(e.role for e in ROLES if e.role not in applies)
    assert writer.get(f"/api/prompts/{other}").json()["profile_generation"] is None


def test_a_non_approver_is_refused_and_nothing_is_written(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    c = inbox_client(_write_app(tmp_path, ["alice"]), loopback_peer=True)
    c.headers.update(_login(c))
    r = c.post("/api/prompts/dispatch/versions", json={"body": _BODY})
    assert r.status_code == 403
    assert c.get("/api/prompts/dispatch/versions").json()["versions"] == []


def test_an_empty_approver_list_refuses_an_approval_with_the_setting_named(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profiles fail closed (#3086): the 403 tells the operator what to set."""
    monkeypatch.setenv("KEW_ACTOR", "alice")
    app = _write_app(tmp_path, [])
    seeded = DispatchConfig()
    seeded.approval.approvers = ["alice"]
    with SqliteRunStore(str(tmp_path / "kew.db")) as store:
        profiles.propose(
            store,
            role="dispatch",
            body=_BODY,
            config=seeded,
            actor=Actor("human", "alice"),
        )
    c = inbox_client(app, loopback_peer=True)
    c.headers.update(_login(c))

    r = c.post("/api/prompts/dispatch/versions/1/approve", json={"generation": 0})

    assert r.status_code == 403
    assert r.json()["detail"] == (
        "Set [approval].approvers in kew.toml before deciding prompt profiles."
    )
    history = c.get("/api/prompts/dispatch/versions").json()
    assert [(v["version"], v["state"]) for v in history["versions"]] == [
        (1, "proposed")
    ]
    assert history["active_version"] is None


@pytest.mark.parametrize(
    ("actor", "approvers", "decides"),
    [("alice", ["alice"], True), ("mallory", ["alice"], False), ("bob", [], False)],
)
def test_the_history_says_whether_the_viewer_can_decide(
    tmp_path: Path,
    inbox_client,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    approvers: list[str],
    decides: bool,
) -> None:
    """The console offers Approve and Decline only to someone the approver
    gate would let decide; a read and a write's answer say the same."""
    monkeypatch.setenv("KEW_ACTOR", actor)
    c = inbox_client(_write_app(tmp_path, approvers), loopback_peer=True)
    c.headers.update(_login(c))
    assert c.get("/api/prompts/dispatch/versions").json()["can_decide"] is decides
    r = c.post("/api/prompts/dispatch/versions", json={"body": _BODY})
    if decides:
        assert r.json()["can_decide"] is True
    else:
        assert r.status_code == 403


def test_a_server_without_config_says_nobody_can_decide(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "alice")
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    app = create_app(
        lambda: _store_factory(db),
        config=None,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    c = inbox_client(app, loopback_peer=True)
    headers = _login(c)
    shown = c.get("/api/prompts/dispatch/versions").json()
    assert shown["can_decide"] is False
    refused = c.post(
        "/api/prompts/dispatch/versions", json={"body": "Be brief."}, headers=headers
    )
    assert refused.status_code == 503
    # The console prints the write's own refusal, not a kew.toml fix it can't take.
    assert shown["decide_refusal"] == refused.json()["detail"]


@pytest.mark.parametrize(
    ("actor", "approvers", "refusal"),
    [
        ("alice", ["alice"], None),
        ("mallory", ["alice"], None),
        (
            "bob",
            [],
            "Set [approval].approvers in kew.toml before deciding prompt profiles.",
        ),
    ],
)
def test_the_history_names_the_fix_when_no_approver_is_configured(
    tmp_path: Path,
    inbox_client,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    approvers: list[str],
    refusal: str | None,
) -> None:
    """With no approvers nobody can write, and the read carries the sentence
    that says how to fix it; an approver and a non-approver get none."""
    monkeypatch.setenv("KEW_ACTOR", actor)
    c = inbox_client(_write_app(tmp_path, approvers), loopback_peer=True)
    c.headers.update(_login(c))
    history = c.get("/api/prompts/dispatch/versions").json()
    assert history["decide_refusal"] == refusal
    r = c.post("/api/prompts/dispatch/versions", json={"body": _BODY})
    if refusal is not None:
        assert r.status_code == 403
        assert r.json()["detail"] == refusal


def test_an_approval_on_a_stale_generation_answers_409(writer) -> None:
    seen = _propose(writer)["generation"]
    _propose(writer, "A second proposal.")
    first = writer.post(
        "/api/prompts/dispatch/versions/2/approve", json={"generation": seen}
    )
    assert first.status_code == 200
    stale = writer.post(
        "/api/prompts/dispatch/versions/1/approve", json={"generation": seen}
    )
    assert stale.status_code == 409
    after = writer.get("/api/prompts/dispatch/versions").json()
    assert after["active_version"] == 2


def test_a_retire_against_a_generation_that_moved_is_refused(writer) -> None:
    """The person saw generation N; another approval moved the role on before
    their retire landed. The retire names N and is refused, writing nothing."""
    first = _propose(writer)
    writer.post(
        "/api/prompts/dispatch/versions/1/approve",
        json={"generation": first["generation"]},
    )
    seen = writer.get("/api/prompts/dispatch/versions").json()
    _propose(writer, "A replacement.")
    writer.post(
        "/api/prompts/dispatch/versions/2/approve",
        json={"generation": seen["generation"]},
    )

    r = writer.post(
        "/api/prompts/dispatch/retire", json={"generation": seen["generation"]}
    )
    assert r.status_code == 409
    after = writer.get("/api/prompts/dispatch/versions").json()
    assert after["active_version"] == 2

    current = writer.post(
        "/api/prompts/dispatch/retire", json={"generation": after["generation"]}
    )
    assert current.status_code == 200
    assert current.json()["active_version"] is None


def test_decline_leaves_the_role_on_what_it_ran(writer) -> None:
    _propose(writer)
    r = writer.post("/api/prompts/dispatch/versions/1/decline")
    assert r.status_code == 200
    body = r.json()
    assert body["active_version"] is None
    assert body["versions"][0]["state"] == "declined"
    again = writer.post("/api/prompts/dispatch/versions/1/decline")
    assert again.status_code == 409


def test_a_body_over_the_limit_answers_400(writer) -> None:
    r = writer.post("/api/prompts/dispatch/versions", json={"body": "x" * 4001})
    assert r.status_code == 400
    assert "4000" in r.json()["detail"]


def test_a_role_no_profile_applies_to_has_no_versions(writer) -> None:
    applies = {e.role for e in ROLES if e.profile_applies}
    other = next(e.role for e in ROLES if e.role not in applies)
    assert writer.get(f"/api/prompts/{other}/versions").status_code == 404
    r = writer.post(f"/api/prompts/{other}/versions", json={"body": _BODY})
    assert r.status_code == 404
    assert writer.get("/api/prompts/no_such_role/versions").status_code == 404


# --- One generation's view (#3025) -------------------------------------------


class _DecisionAfterRowsRead:
    """The served store, with another approver's decision committing just
    after a read of the version rows while ``armed`` lasts: the window in which
    a history read apart from its generation pairs old rows with a new one."""

    def __init__(self, store: SqliteRunStore, race: dict) -> None:
        self._store = store
        self._race = race

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def list_prompt_profile_versions(self, role: str):
        rows = self._store.list_prompt_profile_versions(role)
        if self._race["armed"] > 0:
            self._race["armed"] -= 1
            self._race["decide"]()
        return rows


def _raced_client(tmp_path: Path, inbox_client) -> tuple:
    """A logged-in approver's client, and the race its store reads under."""
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    config = DispatchConfig()
    config.approval.approvers = ["alice"]

    def decide() -> None:
        other = SqliteRunStore(db)
        try:
            proposed = profiles.propose(
                other, role="dispatch", body="Another approver's change.", config=config
            )
            profiles.approve(
                other,
                role="dispatch",
                version=proposed.version,
                generation=profiles.current_generation(other, "dispatch"),
                config=config,
            )
        finally:
            other.close()

    race: dict = {"armed": 0, "decide": decide}

    @contextmanager
    def factory():
        with _store_factory(db) as store:
            yield _DecisionAfterRowsRead(store, race)

    app = create_app(
        factory,
        config=config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    c = inbox_client(app, loopback_peer=True)
    c.headers.update(_login(c))
    return c, race


def test_a_history_read_during_an_approval_is_one_generations_view(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval lands between the rows and the generation of one read. The
    read answers the new rows with the new generation, never the old rows with
    it; the generation the person saw before it still refuses their retire."""
    monkeypatch.setenv("KEW_ACTOR", "alice")
    c, race = _raced_client(tmp_path, inbox_client)
    first = _propose(c)
    approved = c.post(
        "/api/prompts/dispatch/versions/1/approve",
        json={"generation": first["generation"]},
    )
    assert approved.status_code == 200, approved.text
    seen = c.get("/api/prompts/dispatch/versions").json()
    assert (seen["active_version"], seen["generation"]) == (1, 1)

    race["armed"] = 1
    raced = c.get("/api/prompts/dispatch/versions")
    assert raced.status_code == 200, raced.text
    view = raced.json()
    assert race["armed"] == 0, "the approval never landed inside the read"
    active_rows = [v["version"] for v in view["versions"] if v["state"] == "active"]
    assert (view["active_version"], active_rows, view["generation"]) == (2, [2], 2)

    stale = c.post(
        "/api/prompts/dispatch/retire", json={"generation": seen["generation"]}
    )
    assert stale.status_code == 409
    assert c.get("/api/prompts/dispatch/versions").json()["active_version"] == 2


def test_a_history_that_never_settles_answers_409_not_a_split_view(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "alice")
    c, race = _raced_client(tmp_path, inbox_client)
    race["armed"] = 10
    r = c.get("/api/prompts/dispatch/versions")
    assert r.status_code == 409
    assert r.json()["detail"].endswith("Reload to see the new version.")


class _GenerationMovesOnEveryRead:
    """The served store, with the role's generation reading one higher on
    every read while ``moving`` is set: as if a decision landed between any
    two reads, without writing a version of its own."""

    def __init__(self, store: SqliteRunStore, moving: dict) -> None:
        self._store = store
        self._moving = moving

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def prompt_profile_generation(self, role: str) -> int:
        real = self._store.prompt_profile_generation(role)
        if not self._moving["on"]:
            return real
        self._moving["reads"] += 1
        return real + self._moving["reads"]


def test_a_committed_proposal_whose_history_never_settles_answers_503_not_409(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposal commits, then every read of the history after it sees the
    generation move. The answer says the change was saved (503), never that
    nothing was written (409), and the role holds exactly the one proposal."""
    monkeypatch.setenv("KEW_ACTOR", "alice")
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    config = DispatchConfig()
    config.approval.approvers = ["alice"]
    moving = {"on": False, "reads": 0}

    @contextmanager
    def factory():
        with _store_factory(db) as store:
            yield _GenerationMovesOnEveryRead(store, moving)

    app = create_app(
        factory,
        config=config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    c = inbox_client(app, loopback_peer=True)
    c.headers.update(_login(c))

    moving["on"] = True
    r = c.post("/api/prompts/dispatch/versions", json={"body": _BODY})
    assert r.status_code == 503
    assert r.json()["detail"] == (
        "The change was saved, but the new state could not be read. Reload to see it."
    )
    assert moving["reads"] > 0, "the history after the write was never read"

    moving["on"] = False
    after = c.get("/api/prompts/dispatch/versions").json()
    assert [(v["version"], v["state"], v["body"]) for v in after["versions"]] == [
        (1, "proposed", _BODY)
    ]


def test_an_approval_of_a_digest_mismatched_proposal_answers_400_with_its_remedy(
    writer, tmp_path: Path
) -> None:
    from support.prompt_profiles import propose_unchecked

    seen = _propose(writer)["generation"]
    assert (
        writer.post(
            "/api/prompts/dispatch/versions/1/approve", json={"generation": seen}
        ).status_code
        == 200
    )
    with _store_factory(str(tmp_path / "kew.db")) as store:
        propose_unchecked(
            store, "Run the linter twice.", version=2, body_sha256="0" * 64
        )

    refused = writer.post(
        "/api/prompts/dispatch/versions/2/approve", json={"generation": seen + 1}
    )

    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "dispatch profile v2 is refused" in detail
    assert "decline it and propose it again" in detail
    after = writer.get("/api/prompts/dispatch/versions").json()
    assert after["active_version"] == 1
    assert sorted((v["version"], v["state"]) for v in after["versions"]) == [
        (1, "active"),
        (2, "proposed"),
    ]
