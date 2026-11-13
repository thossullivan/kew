"""Tests for the shipped-artifact catalog (kew skills)."""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from support.kew_commands import named_invocations, unknown_invocations

from kew.config import DispatchConfig
from kew.skills_catalog import Artifact, artifact_state, compute_sha256, load_manifest

PKG_ROOT = Path(__file__).parent.parent / "src" / "kew"


def test_manifest_loads_and_has_expected_artifacts():
    artifacts = load_manifest()
    names = {a.name for a in artifacts}
    assert {"plan-to-issues", "security-guard"} <= names
    for a in artifacts:
        assert a.kind in ("skill", "hook", "prompt")
        assert a.version >= 1
        assert a.target.startswith(".claude/")


def test_manifest_hashes_match_shipped_content():
    """Integrity: changing a shipped artifact without bumping the manifest fails here.

    On mismatch: bump `version`, append the old sha256 to `previous_sha256`,
    set `sha256` to the printed actual value.
    """
    for a in load_manifest():
        source = PKG_ROOT / a.source
        assert source.exists(), f"{a.name}: missing source {a.source}"
        actual = compute_sha256(source)
        assert actual == a.sha256, f"{a.name}: manifest sha256 stale. Actual: {actual}"


def test_manifest_hash_history_is_disjoint():
    for a in load_manifest():
        assert a.sha256 not in a.previous_sha256


# Every (version, sha256) this repository has shipped as an artifact's CURRENT
# pin. A repo that installed one of them holds that exact content, so the sha
# must stay reachable as history: drop it and `artifact_state` calls the
# untouched copy `locally-modified`, which makes `kew skills update` refuse it
# and `kew setup --with-skills` skip it — the stale skill stays forever.
# Re-pinning an artifact therefore appends BOTH: a row here and the old sha in
# the manifest's `previous_sha256`, with `version` bumped. Append only; a row
# is only wrong if that content never reached main.
# The table has to carry the CURRENT pin too, or it guards only the history it
# happened to be updated for: `test_released_table_covers_the_current_pin`
# fails the re-pin that does not append its own row, and from that row on
# `test_released_shas_stay_updatable` keeps the sha reachable.
_RELEASED: dict[str, tuple[tuple[int, str], ...]] = {
    "plan-to-issues": (
        (1, "e6bb9969adfe38e433879c182762d53206824eb8fc92bb6d05787d83d3e26f6b"),
        (2, "ee22816768ec963600ac19c50b005a497e43a4efbddabab17b86699e6568b358"),
        (3, "d7f44fffda31b2d143a59d4253855aa99abdcf3251e20e505d1378f2c2ec69de"),
        (4, "b365b83a533a8d08d294bec33c966be7df483318ee50e293d79245e2637f1df2"),
        (5, "9af3a46212de9a2b3388e6e9c5e943cb09e8857ce50afcdaad4d6ff6a06004b2"),
    ),
}


@pytest.mark.parametrize("name", sorted(_RELEASED))
def test_released_shas_stay_updatable(name: str):
    """#2310: re-pinning plan-to-issues over v3 kept `version` at 3 and dropped
    the released sha, so every repo holding v3 read as locally modified."""
    a = next(a for a in load_manifest() if a.name == name)
    known = {a.sha256, *a.previous_sha256}
    for version, sha in _RELEASED[name]:
        assert sha in known, (
            f"{name}: released v{version} sha {sha[:12]} is in neither sha256 nor "
            "previous_sha256 — repos holding it now read as locally-modified; "
            "append it to previous_sha256"
        )
        if sha != a.sha256:
            assert a.version > version, (
                f"{name}: content changed since released v{version} but version "
                f"is still {a.version} — bump it"
            )


@pytest.mark.parametrize("name", sorted(_RELEASED))
def test_released_table_covers_the_current_pin(name: str):
    """#2310: the table above only guards the pins it lists, so a re-pin that
    leaves it alone is unguarded — the round after can drop that sha. Requiring
    the current pin closes the loop: appending the row is part of re-pinning."""
    a = next(a for a in load_manifest() if a.name == name)
    assert (a.version, a.sha256) in _RELEASED[name], (
        f"{name}: current pin v{a.version} {a.sha256[:12]} is not in _RELEASED "
        "— append it there as part of this re-pin, or the next one can drop it "
        "from previous_sha256 with the suite still green"
    )


def test_all_skill_artifacts_have_frontmatter():
    """Every kind=skill artifact must carry name+description frontmatter
    (Claude Code auto-invocation keys off the description)."""
    artifacts = [a for a in load_manifest() if a.kind == "skill"]
    assert {a.name for a in artifacts} >= {
        "plan-to-issues",
        "playbook-skill",
        "verify-parity",
        "handoff",
    }
    for a in artifacts:
        text = (PKG_ROOT / a.source).read_text()
        head, _, _ = text.partition("\n---\n")
        assert head.startswith("---"), f"{a.name}: missing frontmatter"
        assert f"name: {a.name}" in head, f"{a.name}: frontmatter name mismatch"
        assert "description:" in head, f"{a.name}: missing description"


# --- artifact_state tests ---


def _cfg() -> DispatchConfig:
    return DispatchConfig()


def _skill(name: str = "handoff") -> Artifact:
    return next(a for a in load_manifest() if a.name == name)


def _hook() -> Artifact:
    return next(a for a in load_manifest() if a.kind == "hook")


def test_state_not_installed(tmp_path: Path):
    assert artifact_state(_skill(), tmp_path, _cfg()) == "not-installed"


def test_state_current_after_faithful_copy(tmp_path: Path):
    a = _skill()
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_bytes((PKG_ROOT / a.source).read_bytes())
    assert artifact_state(a, tmp_path, _cfg()) == "current"


def test_state_locally_modified_on_unknown_content(tmp_path: Path):
    a = _skill()
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_text("user edited this\n")
    assert artifact_state(a, tmp_path, _cfg()) == "locally-modified"


def test_state_outdated_when_hash_in_history(tmp_path: Path):
    a = _skill()
    old_content = b"previous shipped version\n"
    old_hash = hashlib.sha256(old_content).hexdigest()
    stale = Artifact(
        name=a.name,
        kind=a.kind,
        version=a.version + 1,
        source=a.source,
        target=a.target,
        sha256=a.sha256,
        previous_sha256=(old_hash,),
    )
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old_content)
    assert artifact_state(stale, tmp_path, _cfg()) == "outdated"


def test_hook_state_current_matches_render(tmp_path: Path):
    from kew.security import render_security_hook

    a = _hook()
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_text(render_security_hook(_cfg()))
    assert artifact_state(a, tmp_path, _cfg()) == "current"


def test_hook_state_outdated_when_managed_but_stale(tmp_path: Path):
    a = _hook()
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_text("# managed-by: kew\n# old template body\n")
    assert artifact_state(a, tmp_path, _cfg()) == "outdated"


def test_hook_state_locally_modified_when_user_managed(tmp_path: Path):
    a = _hook()
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/bin/bash\n# my own hook\n")
    assert artifact_state(a, tmp_path, _cfg()) == "locally-modified"


# --- apply/diff operations tests ---


def test_apply_installs_then_is_idempotent(tmp_path: Path):
    from kew.skills_catalog import apply_artifact

    a = _skill()
    assert apply_artifact(a, tmp_path, _cfg()) == "installed"
    assert (tmp_path / a.target).read_bytes() == (PKG_ROOT / a.source).read_bytes()
    assert apply_artifact(a, tmp_path, _cfg()) == "unchanged"


def test_apply_refuses_locally_modified_without_force(tmp_path: Path):
    import pytest

    from kew.skills_catalog import LocallyModifiedError, apply_artifact

    a = _skill()
    apply_artifact(a, tmp_path, _cfg())
    (tmp_path / a.target).write_text("user edit\n")
    with pytest.raises(LocallyModifiedError):
        apply_artifact(a, tmp_path, _cfg())
    assert apply_artifact(a, tmp_path, _cfg(), force=True) == "updated"
    assert artifact_state(a, tmp_path, _cfg()) == "current"


def test_apply_updates_outdated(tmp_path: Path):
    from kew.skills_catalog import apply_artifact

    a = _skill()
    old_content = b"previous shipped version\n"
    old_hash = hashlib.sha256(old_content).hexdigest()
    stale = Artifact(
        name=a.name,
        kind=a.kind,
        version=a.version + 1,
        source=a.source,
        target=a.target,
        sha256=a.sha256,
        previous_sha256=(old_hash,),
    )
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old_content)
    assert apply_artifact(stale, tmp_path, _cfg()) == "updated"
    assert artifact_state(stale, tmp_path, _cfg()) == "current"


def test_apply_hook_wires_settings_and_is_executable(tmp_path: Path):
    import json
    import os

    from kew.skills_catalog import apply_artifact

    a = _hook()
    assert apply_artifact(a, tmp_path, _cfg()) == "installed"
    hook = tmp_path / a.target
    assert os.access(hook, os.X_OK)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    matchers = {e["matcher"] for e in settings["hooks"]["PreToolUse"]}
    assert {"Bash", "Write", "Edit"} <= matchers
    # settings merge is idempotent
    apply_artifact(a, tmp_path, _cfg(), force=True)
    settings2 = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings == settings2


def test_apply_hook_refuses_user_managed_without_force(tmp_path: Path):
    import pytest

    from kew.skills_catalog import LocallyModifiedError, apply_artifact

    a = _hook()
    dest = tmp_path / a.target
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/bin/bash\n# my own hook\n")
    with pytest.raises(LocallyModifiedError):
        apply_artifact(a, tmp_path, _cfg())


def test_diff_reports_drift(tmp_path: Path):
    from kew.skills_catalog import apply_artifact, diff_artifact

    a = _skill()
    apply_artifact(a, tmp_path, _cfg())
    assert diff_artifact(a, tmp_path, _cfg()) == ""
    (tmp_path / a.target).write_text("user edit\n")
    d = diff_artifact(a, tmp_path, _cfg())
    assert "user edit" in d and a.target in d


# --- internal docs directory convention (R15) ---
#
# The plan/handoff/audit skills write internal working material. It must not
# land in the project's published `docs/` tree — that tree is what MkDocs builds
# and deploys, and the 2026-08-06 leak (PR #1407) is what happens when internal
# pages reach it. The convention that replaced those paths: resolve an internal
# docs directory, defaulting to `.kew/docs/`, and write under it.

REPO_ROOT = Path(__file__).parent.parent.parent

# Skills that write internal working material and therefore carry the block.
INTERNAL_DOCS_SKILLS = ("handoff", "plan-to-issues")

# A pointer into the PUBLIC docs tree. The lookbehind spares `.kew/docs/plans/`
# and any other internal-docs-rooted path, which is the whole point of the
# convention. Written as a regex, not as a literal path, so this file does not
# itself become the stale reference `scripts/check-docs.sh` hunts for.
PUBLIC_DOCS_POINTER = re.compile(
    r"(?<!\.kew/)docs/(plans|handoff|audit|spikes|superpowers|receipts)/"
)


def _shipped_skill_text(name: str) -> str:
    a = next(a for a in load_manifest() if a.name == name)
    return (PKG_ROOT / a.source).read_text()


@pytest.mark.parametrize("name", INTERNAL_DOCS_SKILLS)
def test_shipped_skill_resolves_an_internal_docs_directory(name: str):
    text = _shipped_skill_text(name)
    assert "internal docs directory" in text, (
        f"{name}: no internal-docs-directory block"
    )
    assert "`.kew/docs/`" in text, f"{name}: does not name the default `.kew/docs/`"
    assert "`CLAUDE.md`" in text and "`AGENTS.md`" in text, (
        f"{name}: does not say where a project declares its own directory"
    )


@pytest.mark.parametrize("name", INTERNAL_DOCS_SKILLS)
def test_shipped_skill_never_writes_into_the_public_docs_tree(name: str):
    hits = PUBLIC_DOCS_POINTER.findall(_shipped_skill_text(name))
    assert not hits, f"{name}: still points into the published docs tree: {hits}"


# The copies Claude Code actually loads in this repository: the two deployed
# shipped skills plus the repo-local audit skill, which has no shipped source.
DEPLOYED_INTERNAL_DOCS_SKILLS = ("audit", "handoff", "plan-to-issues")


@pytest.mark.parametrize("name", DEPLOYED_INTERNAL_DOCS_SKILLS)
def test_deployed_skill_never_writes_into_the_public_docs_tree(name: str):
    text = (REPO_ROOT / ".claude" / "skills" / name / "SKILL.md").read_text()
    hits = PUBLIC_DOCS_POINTER.findall(text)
    assert not hits, (
        f".claude/skills/{name}: still points into the published docs tree: {hits}"
    )
    assert "internal docs directory" in text


@pytest.mark.parametrize("name", INTERNAL_DOCS_SKILLS)
def test_deployed_copy_matches_the_shipped_skill(name: str):
    deployed = (REPO_ROOT / ".claude" / "skills" / name / "SKILL.md").read_text()
    assert deployed == _shipped_skill_text(name), (
        f".claude/skills/{name} differs from the shipped source: regenerate the "
        f"manifest pin for {name}, then run `kew skills update {name}` from the "
        "repo root (with a matching pin `update` reports `unchanged`)"
    )


def test_skills_doc_documents_the_internal_docs_directory():
    text = (REPO_ROOT / "docs" / "skills.md").read_text()
    assert "internal docs directory" in text
    assert "`.kew/docs/`" in text
    # It is a skill-level convention: no kew command reads it, and saying so is
    # the difference between documenting a convention and claiming a feature.
    assert "not a kew runtime setting" in text
    hits = PUBLIC_DOCS_POINTER.findall(text)
    assert not hits, f"docs/skills.md still points into the published docs tree: {hits}"


def test_docs_gate_no_longer_blanket_excludes_the_skills_directories():
    """R15 removed the `_skills`/`skills` exclusions from the stale-reference
    grep, so a skill that regresses to a public docs path fails the gate."""
    gate = (REPO_ROOT / "scripts" / "check-docs.sh").read_text()
    assert "--exclude-dir=_skills" not in gate
    assert "--exclude-dir=skills" not in gate
    # `--exclude-dir=audit` matched the basename at any depth and hid
    # `.claude/skills/audit/`; the held packs have left the tree, so no audit
    # path is excluded at all.
    assert "--exclude-dir=audit" not in gate
    assert "'^docs/audit/'" not in gate
    assert "skills/handoff/SKILL" not in gate
    assert "skills/plan-to-issues/SKILL" not in gate


# Synthesized rather than written literally: this file is inside the docs gate's
# own scan set, so an embedded `docs/<deleted-tree>/` string would make the test
# the very violation it exists to provoke.
_PUBLIC_POINTER_FIXTURE = "docs/" + "plans/scratch-r15.md"
_STALE_STAGE_ERROR = "references into a deleted internal docs tree remain"


def _scratch_repo(tmp_path: Path) -> Path:
    scratch = tmp_path / "repo"
    subprocess.run(
        # Depth 1 over file://, not --shared: see test_docs_truthfulness._scratch_repo.
        [
            "git",
            "-c",
            "advice.detachedHead=false",
            "clone",
            "--quiet",
            "--depth",
            "1",
            f"file://{REPO_ROOT}",
            str(scratch),
        ],
        check=True,
    )
    # Replay the checkout's uncommitted tracked changes (empty on a clean
    # checkout) so the gate runs over the candidate tree, not HEAD's.
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    if diff:
        subprocess.run(["git", "apply", "--index"], cwd=scratch, input=diff, check=True)
    return scratch


def _run_docs_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/check-docs.sh"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )


class TestDocsGateSeparatesTheInternalDocsRoot:
    """The gate hunts pointers into the deleted public `docs/<tree>/` dirs.
    `.kew/docs/plans/` shares both the `docs/` segment and the leaf name but is
    a different root — the internal docs directory the skills write to. One
    scratch skill file, written both ways, proves the gate tells them apart."""

    FIXTURE = ".claude/skills/scratch-r15/SKILL.md"

    def _write(self, repo: Path, body: str) -> None:
        target = repo / self.FIXTURE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    def test_internal_docs_pointer_is_not_a_stale_reference(
        self, tmp_path: Path
    ) -> None:
        repo = _scratch_repo(tmp_path)
        self._write(repo, "Plans live in `.kew/docs/plans/`.\n")

        output = (lambda r: r.stdout + r.stderr)(_run_docs_gate(repo))

        # Exit code is deliberately not asserted: later stages build the docs
        # and need the network. The stale-reference stage runs first and is the
        # one under test.
        assert _STALE_STAGE_ERROR not in output, output

    def test_public_docs_pointer_in_a_skill_still_fails(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        self._write(repo, f"Plans live in `{_PUBLIC_POINTER_FIXTURE}`.\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, (
            "check-docs.sh accepted a skill pointing into a deleted public docs tree"
        )
        assert _STALE_STAGE_ERROR in output, output
        assert self.FIXTURE in output, output


# --- operate-kew (#2507) ---


def test_skills_install_operate_kew_matches_manifest_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["skills", "install", "operate-kew"])

    assert result.exit_code == 0, result.output
    a = _skill("operate-kew")
    installed = tmp_path / a.target
    assert installed == tmp_path / ".claude/skills/operate-kew/SKILL.md"
    assert compute_sha256(installed) == a.sha256
    assert artifact_state(a, tmp_path, _cfg()) == "current"


def test_operate_kew_skill_commands_exist():
    """Every `kew ...` the skill puts in code resolves in `kew schema`, flags too."""
    text = _shipped_skill_text("operate-kew")
    named = {tokens[0] for tokens in named_invocations(text)}
    assert {"doctor", "init", "schema", "setup", "approve", "deny"} <= named
    assert unknown_invocations(text) == []


def test_skill_commands_exist_check_refuses_what_does_not_ship():
    """The checker above is not vacuous: an unknown command or flag is caught."""
    text = (
        "`kew frobnicate`\n\n"
        "```bash\nkew doctor --no-such-flag\nkew doctor --json\n```\n"
    )
    assert unknown_invocations(text) == [
        "kew doctor --no-such-flag",
        "kew frobnicate",
    ]


@pytest.mark.parametrize(
    "pattern",
    [
        "removed or loosened asserts",
        "deleted tests",
        "added skip/xfail",
        "changed expected values",
        "test-only fix rounds",
        "`mock.ANY`",
        "broadened `pytest.raises`",
        "production code that detects a test run",
    ],
)
def test_adversarial_reviewer_names_each_test_change_pattern(pattern: str):
    """#2486: the reviewer prompt names every test-weakening pattern it checks."""
    assert pattern in _shipped_skill_text("adversarial-reviewer")


def test_adversarial_reviewer_states_the_test_changes_contract():
    """#2486: the prompt tells the reviewer what the parser holds a READY to."""
    text = _shipped_skill_text("adversarial-reviewer")
    for needle in ('"test_changes"', "ACCEPTED", "REJECTED", "**Test changes**"):
        assert needle in text


def test_adversarial_reviewer_states_the_declaration_finding_contract():
    """#2872: a declaration-only round is read off the reviewer's structured
    ``kind``/``node_ids`` answer, so the prompt must tell it to give one."""
    text = _shipped_skill_text("adversarial-reviewer")
    for needle in ('"kind": "declaration"', '"node_ids"', "no `kind`"):
        assert needle in text
