"""``GET /api/prompts/<role>`` previews the role's active approved profile (#3023).

The preview reads the role's active row off the host store the way a launch
does, but never through launch admission: a row the launch would refuse
previews the base prompt and says why, and the read still answers 200.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from support.prompt_profiles import activate_unchecked

from kew.audit import Actor
from kew.config import DispatchConfig
from kew.prompt_previews import PREVIEW_RENDERERS
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.service import prompt_profiles as profiles

ALICE = Actor("human", "alice")

#: Each profile role and the fixed sections its launch renders after the
#: addendum: the safety rules and publication, or the reviewer's host sections.
_FIXED_SECTIONS = {
    "dispatch": ("## Safety Rules",),
    "adversarial_review": ("## Authoritative PR diff boundary",),
    "review_remediation": ("## Safety Rules", "## Instructions"),
    "resolve": ("## Safety Rules", "## Instructions"),
    "ci_fix": ("## Safety Rules", "## Instructions"),
}
ROLES = tuple(_FIXED_SECTIONS)


def _body(role: str) -> str:
    return f"Operator note for {role}: keep every change minimal."


@contextmanager
def _store_factory(db_path: str):
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def config() -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.approval.approvers = ["alice"]
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "dispatch.db")


@pytest.fixture
def client(tmp_path: Path, db: str, config: DispatchConfig, inbox_client) -> Any:
    app = create_app(
        lambda: _store_factory(db),
        config=config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    return inbox_client(app, loopback_peer=True)


def _activate(db: str, config: DispatchConfig, role: str) -> None:
    with SqliteRunStore(db) as store:
        profiles.propose(store, role=role, body=_body(role), config=config, actor=ALICE)
        profiles.approve(
            store,
            role=role,
            version=1,
            generation=profiles.current_generation(store, role),
            config=config,
            actor=ALICE,
        )


def _retire(db: str, config: DispatchConfig, role: str) -> None:
    with SqliteRunStore(db) as store:
        profiles.retire(
            store,
            role=role,
            generation=profiles.current_generation(store, role),
            config=config,
            actor=ALICE,
        )


def _preview(client: Any, role: str) -> dict[str, Any]:
    response = client.get(f"/api/prompts/{role}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["preview_sha256"]
        == hashlib.sha256(body["preview"].encode("utf-8")).hexdigest()
    )
    return body


@pytest.mark.parametrize("role", ROLES)
def test_an_active_profile_previews_before_every_fixed_section(
    client: Any, db: str, config: DispatchConfig, role: str
) -> None:
    base = _preview(client, role)
    _activate(db, config, role)

    served = _preview(client, role)

    preview = served["preview"]
    heading = f"## Operator instructions — {role} profile v1"
    assert preview.count(heading) == 1
    assert preview.count(_body(role)) == 1
    for section in _FIXED_SECTIONS[role]:
        assert preview.index(_body(role)) < preview.index(section), section
    assert served["variant"]["profile"] == "v1"
    assert served["preview_sha256"] != base["preview_sha256"]


@pytest.mark.parametrize("role", ROLES)
def test_a_retired_profile_previews_the_base_byte_for_byte(
    client: Any, db: str, config: DispatchConfig, role: str
) -> None:
    base = _preview(client, role)
    _activate(db, config, role)
    _retire(db, config, role)

    served = _preview(client, role)

    assert served["preview"] == base["preview"]
    assert served["preview_sha256"] == base["preview_sha256"]
    assert served["variant"]["profile"] == "none"
    assert base["variant"]["profile"] == "none"


@pytest.mark.parametrize("role", ROLES)
def test_a_profile_the_launch_would_refuse_previews_the_base_and_says_why(
    client: Any, db: str, role: str
) -> None:
    base = _preview(client, role)
    with SqliteRunStore(db) as store:
        activate_unchecked(store, _body(role), role, body_sha256="0" * 64)

    served = _preview(client, role)

    assert served["preview"] == base["preview"]
    assert _body(role) not in served["preview"]
    assert served["variant"]["profile"] == (
        "refused: its body does not match the approved digest; retire the"
        " profile and propose it again"
    )


def test_a_role_no_profile_applies_to_reads_no_profile(
    client: Any, db: str, config: DispatchConfig
) -> None:
    _activate(db, config, "dispatch")

    served = _preview(client, "triage")

    assert "profile" not in served["variant"]
    assert "Operator instructions" not in served["preview"]


def test_one_roles_profile_never_reaches_another_roles_preview(
    client: Any, db: str, config: DispatchConfig
) -> None:
    _activate(db, config, "ci_fix")

    for role in ROLES:
        if role == "ci_fix":
            continue
        served = _preview(client, role)
        assert _body("ci_fix") not in served["preview"], role
        assert served["variant"]["profile"] == "none", role


def test_a_dispatch_profile_under_a_custom_template_previews_the_refusal(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client
) -> None:
    template = tmp_path / "custom.md.j2"
    template.write_text("Custom prompt for {{ issue_num }}\n")
    served_config = config.model_copy(deep=True)
    served_config.prompt.template = str(template)
    app = create_app(
        lambda: _store_factory(db),
        config=served_config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    client = inbox_client(app, loopback_peer=True)
    base = _preview(client, "dispatch")
    _activate(db, config, "dispatch")

    served = _preview(client, "dispatch")

    # The launch refuses this pair, so the preview shows the base, not v1.
    assert served["preview"] == base["preview"]
    assert _body("dispatch") not in served["preview"]
    assert served["variant"]["profile"] == (
        "refused: the dispatch prompt profile v1 is active and only the built-in"
        " prompt renders it; retire the profile or remove [prompt] template from"
        " kew.toml"
    )


def test_a_dispatch_profile_under_host_publication_and_an_append_previews_the_refusal(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client, client: Any
) -> None:
    base_client = client
    served_config = config.model_copy(deep=True)
    served_config.prompt.append = "Run extra checks"
    app = create_app(
        lambda: _store_factory(db),
        config=served_config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    client = inbox_client(app, loopback_peer=True)
    base = _preview(base_client, "dispatch")
    _activate(db, config, "dispatch")

    served = _preview(client, "dispatch")

    # The launch's composer refuses an append under host publication, so it
    # builds no prompt: the preview is the base, says why, and still names the
    # active version.
    assert served["variant"]["publication"] == "host"
    assert served["preview"] == base["preview"]
    assert "Run extra checks" not in served["preview"]
    assert _body("dispatch") not in served["preview"]
    assert served["addendum"] is None
    assert served["variant"]["refusal"] == (
        "host publication requires the built-in prompt without appended instructions"
    )
    assert served["variant"]["profile"] == "v1"


def test_a_custom_template_that_raises_previews_the_refusal_not_a_500(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client
) -> None:
    template = tmp_path / "custom.md.j2"
    template.write_text("{{ 1 / 0 }}\n")
    checkout = config.model_copy(deep=True)
    checkout.publication.publish = "checkout"
    served_config = checkout.model_copy(deep=True)
    served_config.prompt.template = str(template)

    def _client(cfg: DispatchConfig) -> Any:
        app = create_app(
            lambda: _store_factory(db),
            config=cfg,
            repo_dir=tmp_path,
            token="TESTTOKEN",
            sessions=SessionStore(),
            prompt_renderers=PREVIEW_RENDERERS,
        )
        return inbox_client(app, loopback_peer=True)

    base = _preview(_client(checkout), "dispatch")
    served = _preview(_client(served_config), "dispatch")

    assert served["preview"] == base["preview"]
    assert served["variant"]["refusal"] == (
        "the [prompt] template cannot be rendered: ZeroDivisionError: division by zero"
    )


def _served_client(
    tmp_path: Path, db: str, cfg: DispatchConfig, inbox_client: Any
) -> Any:
    app = create_app(
        lambda: _store_factory(db),
        config=cfg,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    return inbox_client(app, loopback_peer=True)


def test_a_refused_profile_beside_a_custom_template_says_prompt_config_is_not_rendered(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client
) -> None:
    template = tmp_path / "custom.md.j2"
    template.write_text("Custom prompt for {{ issue_num }}\n")
    served_config = config.model_copy(deep=True)
    served_config.publication.publish = "checkout"
    served_config.prompt.template = str(template)
    client = _served_client(tmp_path, db, served_config, inbox_client)
    assert _preview(client, "dispatch")["prompt_config_rendered"] is True
    _activate(db, config, "dispatch")

    served = _preview(client, "dispatch")

    # The profile is refused, so the preview falls back to the built-in base:
    # the answer must not claim the served [prompt] produced this text.
    assert served["variant"]["profile"].startswith("refused: ")
    assert "refusal" not in served["variant"]
    assert "Custom prompt for 1" not in served["preview"]
    assert served["prompt_config_rendered"] is False


def test_a_checkout_append_preview_says_the_prompt_config_is_rendered(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client
) -> None:
    served_config = config.model_copy(deep=True)
    served_config.publication.publish = "checkout"
    served_config.prompt.append = "Run extra checks"
    client = _served_client(tmp_path, db, served_config, inbox_client)
    _activate(db, config, "dispatch")

    served = _preview(client, "dispatch")

    assert "Run extra checks" in served["preview"]
    assert served["variant"]["profile"] == "v1"
    assert served["prompt_config_rendered"] is True


def test_a_composer_refusal_says_the_prompt_config_is_not_rendered(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client
) -> None:
    served_config = config.model_copy(deep=True)
    served_config.prompt.append = "Run extra checks"
    client = _served_client(tmp_path, db, served_config, inbox_client)

    served = _preview(client, "dispatch")

    assert served["variant"]["refusal"]
    assert "Run extra checks" not in served["preview"]
    assert served["prompt_config_rendered"] is False


def test_a_containment_refusal_says_the_prompt_config_is_not_rendered(
    tmp_path: Path, db: str, config: DispatchConfig, inbox_client
) -> None:
    # The sandbox off without the skip flag: the launch refuses before it builds
    # a prompt, and the renderer says so without raising.
    served_config = config.model_copy(deep=True)
    served_config.sandbox.enabled = False
    served_config.sandbox.dangerously_skip_containment = False
    client = _served_client(tmp_path, db, served_config, inbox_client)

    served = _preview(client, "dispatch")

    assert served["variant"]["posture"] == "refused"
    assert served["variant"]["refusal"]
    assert served["prompt_config_rendered"] is False


def test_a_role_prompt_config_never_applies_to_says_it_is_not_rendered(
    client: Any,
) -> None:
    assert _preview(client, "triage")["prompt_config_rendered"] is False
