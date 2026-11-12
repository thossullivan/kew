"""`kew prompts`: list, show and decide prompt profile versions (#3021).

Every decision goes through :mod:`kew.service.prompt_profiles`; these tests
read the outcome off the CLI's output, its exit code and the ledger rows the
service wrote.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from support.reviewer_artifact import commit_reviewer_artifact, git_repo, lose_blob
from typer.testing import CliRunner

from kew import run_store
from kew.cli import app
from kew.ledger_identity import open_managed_store, registry_paths

runner = CliRunner()


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose kew.toml names alice its one approver; returns its ledger."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "kew.toml").write_text(
        '[logging]\ndir = "logs"\n\n[approval]\napprovers = ["alice"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_ACTOR", "alice")
    return tmp_path / "logs" / "dispatch.db"


def _versions(db_path: Path, role: str = "dispatch") -> list[tuple[int, str]]:
    root = db_path.parent.parent
    with open_managed_store(
        project_root=root,
        db_path=db_path,
        anchors_path=root / "audit-anchors.jsonl",
    ) as store:
        return [
            (row.version, row.state) for row in store.list_prompt_profile_versions(role)
        ]


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["prompts", *args])


def _propose(tmp_path: Path, body: str = "Be terse.") -> Any:
    path = tmp_path / "addendum.md"
    path.write_text(body)
    return _invoke("propose", "dispatch", "--file", str(path))


def _listed(role: str) -> dict[str, Any]:
    result = _invoke("list", "--json")
    assert result.exit_code == 0, result.output
    return next(r for r in json.loads(result.stdout) if r["role"] == role)


def test_propose_then_approve_prints_the_version_and_list_shows_it_active(
    ledger: Path, tmp_path: Path
) -> None:
    proposed = _propose(tmp_path)
    assert proposed.exit_code == 0, proposed.output
    assert "dispatch profile v1 proposed" in proposed.output

    approved = _invoke("approve", "dispatch", "1", "--generation", "0")

    assert approved.exit_code == 0, approved.output
    assert "dispatch profile v1 active" in approved.output
    listing = _invoke("list")
    assert listing.exit_code == 0, listing.output
    dispatch_line = next(
        line for line in listing.output.splitlines() if line.startswith("dispatch ")
    )
    assert "active=v1" in dispatch_line
    assert _listed("dispatch")["active_version"] == 1
    assert _versions(ledger) == [(1, "active")]


def test_list_names_every_role_with_its_source_and_none_without_a_profile(
    ledger: Path,
) -> None:
    result = _invoke("list")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    review = next(line for line in lines if line.startswith("review_remediation "))
    assert "packaged-template:review.md.j2" in review
    assert "active=none" in review
    triage = next(line for line in lines if line.startswith("triage "))
    assert "profiles do not apply" in triage


def test_approve_by_a_non_approver_is_refused_and_writes_nothing(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _propose(tmp_path).exit_code == 0
    monkeypatch.setenv("KEW_ACTOR", "mallory")

    result = _invoke("approve", "dispatch", "1", "--generation", "0")

    assert result.exit_code == 2
    assert "actor 'mallory' is not in [approval].approvers" in result.output
    assert _versions(ledger) == [(1, "proposed")]


def test_propose_by_a_non_approver_is_refused_and_writes_nothing(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "mallory")

    result = _propose(tmp_path)

    assert result.exit_code == 2
    assert "actor 'mallory' is not in [approval].approvers" in result.output
    assert _versions(ledger) == []


def test_an_empty_approver_list_refuses_propose_and_approve_and_writes_nothing(
    ledger: Path, tmp_path: Path
) -> None:
    """Profiles fail closed (#3086): kew.toml with no approvers decides nothing."""
    assert _propose(tmp_path).exit_code == 0
    (tmp_path / "kew.toml").write_text('[logging]\ndir = "logs"\n')
    sentence = "Set [approval].approvers in kew.toml before deciding prompt profiles."

    approved = _invoke("approve", "dispatch", "1", "--generation", "0")
    proposed = _propose(tmp_path, "A second proposal.")

    for result in (approved, proposed):
        assert result.exit_code == 2
        assert sentence in result.output
    assert _versions(ledger) == [(1, "proposed")]


def _tree(root: Path) -> list[str]:
    """Every path under *root*, files and directories, relative and sorted."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_non_approver_on_fresh_project_creates_no_ledger_or_registry(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused propose authorizes before any create-capable open (#3080)."""
    addendum = tmp_path / "addendum.md"
    addendum.write_text("Be terse.")
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    before = _tree(tmp_path)

    result = _invoke("propose", "dispatch", "--file", str(addendum))

    assert result.exit_code == 2
    assert "actor 'mallory' is not in [approval].approvers" in result.output
    assert _tree(tmp_path) == before
    assert not ledger.exists()
    assert not (tmp_path / ".kew").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("approve", "dispatch", "1", "--generation", "0"),
        ("decline", "dispatch", "1"),
        ("retire", "dispatch", "--generation", "0"),
    ],
)
def test_a_refused_decision_on_a_fresh_project_writes_nothing(
    ledger: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    before = _tree(tmp_path)

    result = _invoke(*args)

    assert result.exit_code == 2
    assert "actor 'mallory' is not in [approval].approvers" in result.output
    assert _tree(tmp_path) == before


def test_an_empty_approver_list_on_a_fresh_project_writes_nothing(
    ledger: Path, tmp_path: Path
) -> None:
    (tmp_path / "kew.toml").write_text('[logging]\ndir = "logs"\n')
    addendum = tmp_path / "addendum.md"
    addendum.write_text("Be terse.")
    before = _tree(tmp_path)

    result = _invoke("propose", "dispatch", "--file", str(addendum))

    assert result.exit_code == 2
    assert "Set [approval].approvers in kew.toml" in result.output
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("role", "body", "reason"),
    [
        ("triage", "Be terse.", "may not apply to role 'triage'"),
        ("dispatch", "   ", "may not be empty"),
        ("dispatch", "```\ncode\n", "may not contain a block opener"),
    ],
)
def test_an_approvers_invalid_proposal_on_a_fresh_project_writes_nothing(
    ledger: Path, tmp_path: Path, role: str, body: str, reason: str
) -> None:
    addendum = tmp_path / "addendum.md"
    addendum.write_text(body)
    before = _tree(tmp_path)

    result = _invoke("propose", role, "--file", str(addendum))

    assert result.exit_code == 2
    assert reason in result.output
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    "args",
    [
        ("approve", "dispatch", "1", "--generation", "0"),
        ("decline", "dispatch", "1"),
        ("retire", "dispatch", "--generation", "0"),
    ],
)
def test_an_approver_deciding_with_no_ledger_is_refused_and_creates_none(
    ledger: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    """Only a proposal may create the ledger; nothing else has a version to act on."""
    before = _tree(tmp_path)

    result = _invoke(*args)

    assert result.exit_code == 2
    assert f"no ledger at {ledger}" in result.output
    assert _tree(tmp_path) == before


def test_an_approvers_first_proposal_still_creates_the_ledger(
    ledger: Path, tmp_path: Path
) -> None:
    assert not ledger.exists()

    assert _propose(tmp_path).exit_code == 0

    assert _versions(ledger) == [(1, "proposed")]


def test_retire_after_the_role_moved_on_is_refused_and_keeps_the_new_profile(
    ledger: Path, tmp_path: Path
) -> None:
    assert _propose(tmp_path).exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0
    seen = _listed("dispatch")["generation"]
    # Another decision replaces v1 after the person looked.
    assert _propose(tmp_path, "Be terser.").exit_code == 0
    assert _invoke("approve", "dispatch", "2", "--generation", str(seen)).exit_code == 0

    result = _invoke("retire", "dispatch", "--generation", str(seen))

    assert result.exit_code == 2
    assert "is stale" in result.output
    assert _versions(ledger) == [(1, "retired"), (2, "active")]


def test_retire_at_the_generation_seen_leaves_the_role_on_its_base(
    ledger: Path, tmp_path: Path
) -> None:
    assert _propose(tmp_path).exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0
    seen = _listed("dispatch")["generation"]

    result = _invoke("retire", "dispatch", "--generation", str(seen), "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "retired"
    assert _listed("dispatch")["active_version"] is None


def test_decline_leaves_the_role_unchanged(ledger: Path, tmp_path: Path) -> None:
    assert _propose(tmp_path).exit_code == 0

    result = _invoke("decline", "dispatch", "1")

    assert result.exit_code == 0, result.output
    assert "dispatch profile v1 declined" in result.output
    assert _versions(ledger) == [(1, "declined")]
    assert _listed("dispatch")["active_version"] is None


def test_show_renders_the_dispatch_addendum_before_the_fixed_sections(
    ledger: Path, tmp_path: Path
) -> None:
    assert _propose(tmp_path, "Always run the linter twice.").exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0

    result = _invoke("show", "dispatch", "--json")

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert (shown["active_version"], shown["profile"]) == (1, "v1")
    effective = shown["effective_prompt"]
    heading = effective.index("## Operator instructions — dispatch profile v1")
    body = effective.index("Always run the linter twice.")
    safety = effective.index("## Safety Rules")
    precedence = effective.index("take precedence over operator instructions")
    assert heading < body < safety < precedence
    assert "Always run the linter twice." not in shown["base_prompt"]


def test_show_renders_the_reviewer_addendum_before_the_fixed_sections(
    ledger: Path, tmp_path: Path
) -> None:
    path = tmp_path / "addendum.md"
    path.write_text("Flag every unindexed query.")
    assert _invoke("propose", "adversarial_review", "--file", str(path)).exit_code == 0
    assert (
        _invoke("approve", "adversarial_review", "1", "--generation", "0").exit_code
        == 0
    )

    result = _invoke("show", "adversarial_review", "--json")

    assert result.exit_code == 0, result.output
    effective = json.loads(result.stdout)["effective_prompt"]
    body = effective.index("Flag every unindexed query.")
    precedence = effective.index("take precedence over the operator instructions")
    rubric = effective.index("name: adversarial-reviewer")
    boundary = effective.index("## Authoritative PR diff boundary")
    assert body < precedence < rubric < boundary


def test_show_of_a_refused_profile_prints_the_base_and_says_so(ledger: Path) -> None:
    from support.prompt_profiles import activate_unchecked

    root = ledger.parent.parent
    with open_managed_store(
        project_root=root, db_path=ledger, anchors_path=root / "audit-anchors.jsonl"
    ) as store:
        activate_unchecked(store, "Always run the linter twice.", body_sha256="0" * 64)

    result = _invoke("show", "dispatch", "--json")

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert shown["profile"].startswith("refused:")
    assert shown["effective_prompt"] == shown["base_prompt"]
    assert "Always run the linter twice." not in shown["effective_prompt"]


def test_show_of_an_unknown_role_exits_two(ledger: Path) -> None:
    result = _invoke("show", "nobody")

    assert result.exit_code == 2
    assert "no agent role is named 'nobody'" in result.output


def test_approve_of_a_version_that_is_not_proposed_exits_two(
    ledger: Path, tmp_path: Path
) -> None:
    assert _propose(tmp_path).exit_code == 0
    assert _invoke("decline", "dispatch", "1").exit_code == 0

    result = _invoke("approve", "dispatch", "1", "--generation", "0", "--json")

    assert result.exit_code == 2
    assert "is declined, not proposed" in json.loads(result.stdout)["reason"]


def test_schema_lists_every_prompts_command_with_json_support() -> None:
    result = runner.invoke(app, ["schema"])

    assert result.exit_code == 0, result.output
    listed = {
        entry["command"]: entry
        for entry in json.loads(result.stdout)["commands"]
        if entry["path"][0] == "prompts"
    }
    verbs = ("list", "show", "propose", "approve", "decline", "retire")
    assert set(listed) == {f"kew prompts {verb}" for verb in verbs}
    assert all(entry["json"] for entry in listed.values())


def _project(root: Path, reviewer_prompt: str) -> Path:
    """A project at *root* with alice approving and its own deployed reviewer prompt."""
    (root / "logs").mkdir(parents=True)
    (root / "kew.toml").write_text(
        '[logging]\ndir = "logs"\n\n[approval]\napprovers = ["alice"]\n'
    )
    commit_reviewer_artifact(git_repo(root), reviewer_prompt)
    return root


def test_list_and_show_read_the_selected_project_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = _project(tmp_path / "a", "Reviewer prompt of project A.\n")
    project_b = _project(tmp_path / "b", "Reviewer prompt of project B.\n")
    monkeypatch.setenv("KEW_ACTOR", "alice")
    addendum = tmp_path / "addendum.md"
    addendum.write_text("Project A reviewer addendum.")
    monkeypatch.chdir(project_a)
    assert (
        _invoke("propose", "adversarial_review", "--file", str(addendum)).exit_code == 0
    )
    assert (
        _invoke("approve", "adversarial_review", "1", "--generation", "0").exit_code
        == 0
    )
    monkeypatch.chdir(project_b)

    listing = runner.invoke(
        app, ["--project", str(project_a), "prompts", "list", "--json"]
    )
    shown = runner.invoke(
        app, ["--project", str(project_a), "prompts", "show", "adversarial_review"]
    )

    assert listing.exit_code == 0, listing.output
    reviewer = next(
        r for r in json.loads(listing.stdout) if r["role"] == "adversarial_review"
    )
    assert reviewer["source"] == "deployed-artifact:.claude/kew/adversarial-reviewer.md"
    assert reviewer["active_version"] == 1
    assert shown.exit_code == 0, shown.output
    assert "Reviewer prompt of project A." in shown.output
    assert "Reviewer prompt of project B." not in shown.output
    assert "Project A reviewer addendum." in shown.output
    assert "active=v1" in shown.output


def test_list_and_show_refuse_when_the_deployed_reviewer_copy_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lost = "Reviewer prompt whose object was lost.\n"
    project = _project(tmp_path / "p", lost)
    lose_blob(project, lost)
    monkeypatch.chdir(project)

    listing = _invoke("list", "--json")
    shown = _invoke("show", "adversarial_review", "--json")

    for result in (listing, shown):
        assert result.exit_code == 2, result.output
        refusal = json.loads(result.stdout)
        assert refusal["outcome"] == "refused"
        assert ".claude/kew/adversarial-reviewer.md" in refusal["reason"]


def _managed(ledger: Path) -> Any:
    root = ledger.parent.parent
    return open_managed_store(
        project_root=root, db_path=ledger, anchors_path=root / "audit-anchors.jsonl"
    )


def test_show_renders_with_the_stored_config_overrides(ledger: Path) -> None:
    """A launch reloads its configuration with the store's live overrides, so
    the preview of what it renders must too."""
    toml = ledger.parent.parent / "kew.toml"
    toml.write_text(
        toml.read_text() + '\n[routing]\ndefault = { runner = "claude-code" }\n'
    )
    before = _invoke("show", "dispatch", "--json")
    assert before.exit_code == 0, before.output
    assert json.loads(before.stdout)["variant"]["runner"] == "claude-code"
    with _managed(ledger) as store:
        store.set_config_override(
            field="routing.medium",
            value_json='{"runner": "codex"}',
            file_value_json="null",
            set_at="2026-10-20T00:00:00+00:00",
            set_by="test",
        )

    result = _invoke("show", "dispatch", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["variant"]["runner"] == "codex"


def test_show_never_pairs_one_versions_prompt_with_anothers_generation(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval between show's reads must not yield v1's prompt beside v2's
    generation, which a script could use to retire a version it never saw."""
    from kew.cli import prompts as prompts_cli
    from kew.service import prompt_profiles as profiles

    assert _propose(tmp_path, "Version one text.").exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0
    assert _propose(tmp_path, "Version two text.").exit_code == 0
    real_preview = prompts_cli.role_preview
    approved: list[int] = []

    def preview_then_approve_once(*args: Any, **kwargs: Any) -> Any:
        rendered = real_preview(*args, **kwargs)
        if kwargs.get("store") is not None and not approved:
            from kew.audit import Actor
            from kew.config import DispatchConfig

            config = DispatchConfig()
            config.approval.approvers = ["alice"]
            with _managed(ledger) as store:
                profiles.approve(
                    store,
                    role="dispatch",
                    version=2,
                    generation=profiles.current_generation(store, "dispatch"),
                    config=config,
                    actor=Actor("human", "alice"),
                )
            approved.append(2)
        return rendered

    monkeypatch.setattr(prompts_cli, "role_preview", preview_then_approve_once)

    result = _invoke("show", "dispatch", "--json")

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert approved == [2]
    assert shown["active_version"] == 2
    assert "Version two text." in shown["effective_prompt"]
    assert "Version one text." not in shown["effective_prompt"]


def test_show_of_a_registered_ledger_whose_file_is_gone_exits_two(
    ledger: Path, tmp_path: Path
) -> None:
    assert _propose(tmp_path).exit_code == 0
    for suffix in ("", "-wal", "-shm"):
        ledger.with_name(ledger.name + suffix).unlink(missing_ok=True)

    result = _invoke("show", "dispatch")

    assert result.exit_code == 2
    assert "REGISTERED-LEDGER-MISSING" in result.output
    assert "## Safety Rules" not in result.output


def _lose_database(ledger: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        ledger.with_name(ledger.name + suffix).unlink(missing_ok=True)


def _anchored_ledger_lost(ledger: Path, tmp_path: Path, *, registry: bool) -> None:
    """Propose, anchor the chain head, then delete the database (and the
    registry unless *registry*), leaving the audit anchors behind."""
    assert _propose(tmp_path).exit_code == 0
    anchored = runner.invoke(app, ["audit", "anchor"])
    assert anchored.exit_code == 0, anchored.output
    assert (tmp_path / "audit-anchors.jsonl").read_text().strip()
    _lose_database(ledger)
    if not registry:
        registry_paths(tmp_path).registry.unlink()


@pytest.mark.parametrize(
    "args", [("list",), ("list", "--json"), ("show", "dispatch")], ids=" ".join
)
def test_show_refuses_missing_database_and_registry_when_anchors_survive(
    ledger: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    """Surviving anchors are a lost ledger, never a new project: list and show
    refuse as a launch's managed open does instead of reading generation 0."""
    _anchored_ledger_lost(ledger, tmp_path, registry=False)

    result = _invoke(*args)

    assert result.exit_code == 2, result.output
    assert "REGISTERED-LEDGER-MISSING" in result.output
    assert '"generation": 0' not in result.output
    assert "## Safety Rules" not in result.output
    assert not ledger.exists()


@pytest.mark.parametrize("registry", [True, False], ids=["registry", "anchors-only"])
@pytest.mark.parametrize(
    "args",
    [
        ("approve", "dispatch", "1", "--generation", "0"),
        ("decline", "dispatch", "1"),
        ("retire", "dispatch", "--generation", "1"),
    ],
    ids=lambda args: args[0],
)
def test_a_decision_on_a_lost_ledger_is_refused_as_lost_not_unproposed(
    ledger: Path, tmp_path: Path, args: tuple[str, ...], registry: bool
) -> None:
    _anchored_ledger_lost(ledger, tmp_path, registry=registry)
    before = _tree(tmp_path)

    result = _invoke(*args, "--json")

    reason = _refusal(result)["reason"]
    assert reason.startswith("REGISTERED-LEDGER-MISSING")
    assert "no prompt profile has been proposed" not in result.output
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    "args",
    [
        ("approve", "dispatch", "1", "--generation", "0", "--json"),
        ("propose", "dispatch", "--file", "addendum.md", "--json"),
    ],
    ids=lambda args: args[0],
)
def test_a_decision_in_an_unresolvable_git_layout_is_a_json_refusal(
    ledger: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    """A worktree whose gitdir is gone cannot be classified: exit 2 with the
    layout's own reason as a JSON refusal, never a traceback."""
    (tmp_path / "addendum.md").write_text("Be terse.")
    (tmp_path / ".git").write_text("gitdir: missing-gitdir\n")
    before = _tree(tmp_path)

    result = _invoke(*args)

    assert "worktree git directory does not exist" in _refusal(result)["reason"]
    assert "Traceback" not in result.output
    assert _tree(tmp_path) == before


def test_list_in_an_unresolvable_git_layout_exits_two_with_the_reason(
    ledger: Path, tmp_path: Path
) -> None:
    (tmp_path / ".git").write_text("gitdir: missing-gitdir\n")

    result = _invoke("list")

    assert result.exit_code == 2, result.output
    assert "worktree git directory does not exist" in result.output


def test_a_registry_for_another_database_path_is_wrong_ledger_for_every_command(
    ledger: Path, tmp_path: Path
) -> None:
    """[logging].dir moved and the new file is absent while the registered one
    exists: approve, propose and list all report the managed open's
    WRONG-LEDGER, never a lost ledger to restore."""
    assert _propose(tmp_path).exit_code == 0
    (tmp_path / "logs2").mkdir()
    (tmp_path / "kew.toml").write_text(
        '[logging]\ndir = "logs2"\n\n[approval]\napprovers = ["alice"]\n'
    )
    (tmp_path / "second.md").write_text("A second proposal.")
    before = _tree(tmp_path)

    for args in (
        ("approve", "dispatch", "1", "--generation", "0", "--json"),
        ("propose", "dispatch", "--file", "second.md", "--json"),
        ("list",),
    ):
        result = _invoke(*args)
        assert result.exit_code == 2, (args, result.output)
        assert (
            f"WRONG-LEDGER: configured path mismatch; registered path exists"
            f" at {ledger.resolve()}" in result.output
        ), args
        assert "REGISTERED-LEDGER-MISSING" not in result.output, args
    assert _tree(tmp_path) == before
    assert _versions(ledger) == [(1, "proposed")]


def test_an_empty_anchors_file_alone_still_reads_as_a_new_project(
    ledger: Path, tmp_path: Path
) -> None:
    (tmp_path / "audit-anchors.jsonl").write_text("")

    assert _listed("dispatch")["generation"] == 0


def test_show_retries_when_approval_lands_between_state_generation_and_active_read(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile state is one generation's view: an approval between its
    generation read and its active-row read must not pair v2 with generation 1."""
    from kew.audit import Actor
    from kew.cli import prompts as prompts_cli
    from kew.config import DispatchConfig
    from kew.service import prompt_profiles as real_profiles

    assert _propose(tmp_path, "Version one text.").exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0
    assert _propose(tmp_path, "Version two text.").exit_code == 0
    approved: list[int] = []

    class _ApproveBeforeFirstActiveRead:
        def __getattr__(self, name: str) -> Any:
            return getattr(real_profiles, name)

        def active(self, store: Any, role: str) -> Any:
            if not approved:
                config = DispatchConfig()
                config.approval.approvers = ["alice"]
                with _managed(ledger) as writer:
                    real_profiles.approve(
                        writer,
                        role="dispatch",
                        version=2,
                        generation=real_profiles.current_generation(writer, "dispatch"),
                        config=config,
                        actor=Actor("human", "alice"),
                    )
                approved.append(2)
            return real_profiles.active(store, role)

    monkeypatch.setattr(prompts_cli, "profiles", _ApproveBeforeFirstActiveRead())

    result = _invoke("show", "dispatch", "--json")

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert approved == [2]
    generation_after_v2 = shown["generation"]
    assert (shown["active_version"], shown["profile"]) == (2, "v2")
    assert "Version two text." in shown["effective_prompt"]
    with _managed(ledger) as store:
        assert real_profiles.current_generation(store, "dispatch") == (
            generation_after_v2
        )


_HOST_APPEND_REFUSAL = (
    "host publication requires the built-in prompt without appended instructions"
)


def _host_publication_with_an_append_and_v1_active(
    ledger: Path, tmp_path: Path
) -> None:
    toml = ledger.parent.parent / "kew.toml"
    toml.write_text(toml.read_text() + '\n[prompt]\nappend = "Run extra checks"\n')
    assert _propose(tmp_path, "Always run the linter twice.").exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0


def test_show_of_a_launch_the_composer_refuses_prints_the_refusal_not_a_prompt(
    ledger: Path, tmp_path: Path
) -> None:
    _host_publication_with_an_append_and_v1_active(ledger, tmp_path)

    result = _invoke("show", "dispatch")

    assert result.exit_code == 2, result.output
    assert _HOST_APPEND_REFUSAL in result.output
    assert "active=v1" in result.output
    # No prompt is built, so none is printed as the one the launch renders.
    assert "## Safety Rules" not in result.output
    assert "Run extra checks" not in result.output


def test_show_json_of_a_launch_the_composer_refuses_carries_the_refusal(
    ledger: Path, tmp_path: Path
) -> None:
    _host_publication_with_an_append_and_v1_active(ledger, tmp_path)

    result = _invoke("show", "dispatch", "--json")

    assert result.exit_code == 2, result.output
    shown = json.loads(result.stdout)
    assert (shown["outcome"], shown["reason"]) == ("refused", _HOST_APPEND_REFUSAL)
    assert shown["variant"]["refusal"] == _HOST_APPEND_REFUSAL
    assert (shown["active_version"], shown["profile"]) == (1, "v1")


def test_approve_of_a_proposal_whose_digest_does_not_match_exits_two(
    ledger: Path, tmp_path: Path
) -> None:
    from support.prompt_profiles import propose_unchecked

    assert _propose(tmp_path, "first").exit_code == 0
    assert _invoke("approve", "dispatch", "1", "--generation", "0").exit_code == 0
    root = ledger.parent.parent
    with open_managed_store(
        project_root=root, db_path=ledger, anchors_path=root / "audit-anchors.jsonl"
    ) as store:
        propose_unchecked(
            store, "Run the linter twice.", version=2, body_sha256="0" * 64
        )

    result = _invoke("approve", "dispatch", "2", "--generation", "1", "--json")

    assert result.exit_code == 2, result.output
    reason = json.loads(result.stdout)["reason"]
    assert "dispatch profile v2 is refused" in reason
    assert "does not match its recorded digest" in reason
    assert _versions(ledger) == [(1, "active"), (2, "proposed")]
    assert _listed("dispatch")["active_version"] == 1


def _refusal(result: Any) -> dict[str, Any]:
    assert result.exit_code == 2, result.output
    refusal: dict[str, Any] = json.loads(result.stdout)
    assert refusal["outcome"] == "refused"
    return refusal


def test_decision_sqlite_failure_exits_two_and_emits_json_refusal(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _propose(tmp_path).exit_code == 0
    # One attempt: the retry window is the 5s busy timeout, not six of them.
    monkeypatch.setattr(run_store, "_BUSY_MAX_RETRIES", 0)
    holder = sqlite3.connect(ledger)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        result = _invoke("approve", "dispatch", "1", "--generation", "0", "--json")
    finally:
        holder.rollback()
        holder.close()
    assert "locked" in _refusal(result)["reason"]
    assert _versions(ledger) == [(1, "proposed")]


def test_decision_on_a_read_only_ledger_exits_two_and_emits_json_refusal(
    ledger: Path, tmp_path: Path
) -> None:
    assert _propose(tmp_path).exit_code == 0
    before = ledger.read_bytes()
    ledger.chmod(0o444)

    result = _invoke("approve", "dispatch", "1", "--generation", "0", "--json")

    assert "readonly" in _refusal(result)["reason"]
    assert ledger.read_bytes() == before
