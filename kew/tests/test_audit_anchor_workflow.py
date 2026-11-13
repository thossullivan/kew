# tests/test_audit_anchor_workflow.py
"""The audit-anchor workflow's contract with the canonical ledger (#1408).

``KEW_PROJECT`` is the workflow's *sole* state binding: the checked-out CLI
resolves the registered database from it. The workflow may not reconstruct a DB
path in shell, may not consume ``KEW_DISPATCH_DB``, and must clear the inherited
log-dir names so leftover self-hosted runner state cannot outrank the project.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from support.workflow_runners import (
    HOSTED_RUNNER,
    NON_PRIVATE_VISIBILITIES,
    RUNNER_BY_RESOLVED_VISIBILITY,
    SELF_HOSTED,
    resolve_runner,
)

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "audit-anchor.yml"

ANCHOR_INVOCATION = (
    'uv run kew --project "$KEW_PROJECT" audit anchor '
    '--anchors "$GITHUB_WORKSPACE/audit-anchors.jsonl"'
)


def _anchor_step() -> dict:
    """The step that invokes the anchor CLI."""
    data = yaml.safe_load(WF.read_text())
    steps = data["jobs"]["anchor"]["steps"]
    return next(s for s in steps if "audit anchor" in str(s.get("run", "")))


def _guard_script() -> str:
    """The anchor step's shell with the CLI call replaced by a tripwire.

    Running the real command needs the canonical runner's ledger; what the
    guards must prove is that they refuse *before* kew is ever invoked.
    """
    lines = []
    for line in _anchor_step()["run"].splitlines():
        if line.strip().startswith("uv run kew"):
            lines.append('echo "INVOKED-KEW log=${KEW_LOG_DIR-<unset>}"')
        else:
            lines.append(line)
    return "\n".join(lines)


def _run_guards(**env: str) -> subprocess.CompletedProcess:
    """Run the step's shell exactly as `shell: bash` runs it on the runner."""
    return subprocess.run(  # noqa: S603 - fixed argv, workflow shell under test
        # GitHub's default bash shell: --noprofile --norc -eo pipefail.
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _guard_script()],  # noqa: S607
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
        check=False,
    )


def test_workflow_parses():
    data = yaml.safe_load(WF.read_text())
    assert "jobs" in data and "anchor" in data["jobs"]


def test_workflow_guards():
    text = WF.read_text()
    # scheduled + manual triggers
    assert "schedule:" in text and "cron:" in text and "workflow_dispatch:" in text
    # Self-hosted while private, not a repo-variable indirection: the
    # canonical ledger this anchors exists only on that machine, so a hosted
    # fallback would anchor an empty chain and report success.
    assert f"runs-on: {RUNNER_BY_RESOLVED_VISIBILITY}" in text
    # serialize anchor runs, never cancel a push mid-flight
    assert "group: audit-anchor" in text and "cancel-in-progress: false" in text
    # pure-local CLI call, then a guarded commit. Stage-then-check-cached so
    # the first (untracked) anchors file is still detected as a change.
    assert "audit anchor" in text
    assert "git diff --cached --quiet -- audit-anchors.jsonl" in text
    assert "contents: write" in text
    # Shared-clone heal: a leftover sparse checkout must not prune kew/ away.
    assert "git sparse-checkout disable" in text
    assert "kew/pyproject.toml" in text


def test_anchor_job_binds_the_project_on_the_intended_runner():
    """The host checkout and ``KEW_PROJECT`` select the canonical ledger."""
    data = yaml.safe_load(WF.read_text())
    job = data["jobs"]["anchor"]
    step = _anchor_step()

    assert job["runs-on"] == RUNNER_BY_RESOLVED_VISIBILITY
    assert step["env"] == {"KEW_PROJECT": "${{ vars.KEW_PROJECT }}"}
    assert 'if [ -z "${KEW_PROJECT// /}" ]' in step["run"]
    assert 'case "$KEW_PROJECT" in' in step["run"]


def test_checkout_persists_credentials():
    data = yaml.safe_load(WF.read_text())
    steps = data["jobs"]["anchor"]["steps"]
    checkout = next(
        s
        for s in steps
        if isinstance(s.get("uses", ""), str)
        and s.get("uses", "").startswith("actions/checkout")
    )
    assert checkout.get("with", {}).get("persist-credentials") is True


def test_kew_project_is_the_sole_state_binding():
    """No DB path, no log dir — one repository variable selects the ledger."""
    step = _anchor_step()
    assert step["env"] == {"KEW_PROJECT": "${{ vars.KEW_PROJECT }}"}
    # The DB→log-dir split-brain workaround is gone, everywhere in the file.
    text = WF.read_text()
    assert "KEW_DISPATCH_DB" not in text
    assert 'KEW_LOG_DIR="$(dirname' not in text


def test_anchor_step_invokes_the_checked_out_cli_exactly():
    step = _anchor_step()
    assert step["working-directory"] == "kew"
    # Pinned so the guards below run under the same shell the test harness uses.
    assert step["shell"] == "bash"
    run_lines = [line.strip() for line in step["run"].splitlines()]
    assert run_lines.count(ANCHOR_INVOCATION) == 1
    # The workflow never reconstructs a DB path or pre-opens SQLite itself.
    joined = step["run"]
    assert "dispatch.db" not in joined
    assert "sqlite3" not in joined


def test_workflow_requires_absolute_existing_project_and_clears_log_overrides(
    tmp_path,
):
    """The proving test: only an absolute, existing directory reaches kew, and
    an inherited KEW_LOG_DIR is cleared before it does."""
    if shutil.which("bash") is None:  # pragma: no cover - bash is standard here
        pytest.skip("bash unavailable")

    run = _anchor_step()["run"]
    assert "unset KEW_LOG_DIR\n" in run
    # W4.3 row 18: kew no longer reads DISPATCH_LOG_DIR, so nothing clears it.
    assert "DISPATCH_LOG_DIR" not in run

    inherited = {
        "KEW_LOG_DIR": str(tmp_path / "stale-logs"),
        "GITHUB_WORKSPACE": str(tmp_path),
    }

    # empty / unset
    for value in ("", " "):
        rejected = _run_guards(KEW_PROJECT=value, **inherited)
        assert rejected.returncode != 0
        assert "INVOKED-KEW" not in rejected.stdout

    # relative
    rejected = _run_guards(KEW_PROJECT="relative/project", **inherited)
    assert rejected.returncode != 0
    assert "INVOKED-KEW" not in rejected.stdout

    # missing
    rejected = _run_guards(KEW_PROJECT=str(tmp_path / "absent"), **inherited)
    assert rejected.returncode != 0
    assert "INVOKED-KEW" not in rejected.stdout

    # absolute path to a file, not a directory
    not_a_dir = tmp_path / "kew.toml"
    not_a_dir.write_text("", encoding="utf-8")
    rejected = _run_guards(KEW_PROJECT=str(not_a_dir), **inherited)
    assert rejected.returncode != 0
    assert "INVOKED-KEW" not in rejected.stdout

    # absolute + existing directory: kew runs, with the log-dir name cleared
    project = tmp_path / "canonical-project"
    project.mkdir()
    accepted = _run_guards(KEW_PROJECT=str(project), **inherited)
    assert accepted.returncode == 0, accepted.stderr
    assert "INVOKED-KEW" in accepted.stdout
    # The inherited log-dir name does not survive to outrank KEW_PROJECT.
    assert "log=<unset>" in accepted.stdout


def test_log_dir_names_are_cleared_before_the_project_check():
    """Order matters: an inherited log dir must not survive an early exit path
    into a later step, and must be gone by the time kew resolves the DB."""
    run = _anchor_step()["run"]
    unset_at = run.index("unset KEW_LOG_DIR")
    assert unset_at < run.index("uv run kew")


def test_anchor_cli_opens_the_registered_ledger_read_only(tmp_path, monkeypatch):
    """The workflow's exact invocation reaches kew's managed boundary read-only.

    This pins the workflow→CLI contract end to end: `--project` alone selects
    the ledger and the open is read-only, so a scheduled anchor run can never
    create, register, or migrate state on the canonical machine.
    """
    from typer.testing import CliRunner

    from kew.audit import audit_app
    from kew.ledger_identity import open_managed_store
    from kew.run_store import SqliteRunStore

    project = tmp_path / "canonical-project"
    project.mkdir()
    (project / "kew.toml").write_text(
        '[logging]\ndir = "./logs/dispatch"\n', encoding="utf-8"
    )
    logs = project / "logs" / "dispatch"
    logs.mkdir(parents=True)
    db = logs / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.append_audit_event(
            timestamp="2026-08-16T00:00:00+00:00",
            event="dispatch_completed",
            actor_type="human",
            actor_id="tom",
        )
    with open_managed_store(
        project_root=project,
        db_path=db,
        anchors_path=project / "audit-anchors.jsonl",
    ):
        pass

    calls: list[dict] = []
    real_open = open_managed_store

    def _record(**kwargs):
        calls.append(kwargs)
        return real_open(**kwargs)

    monkeypatch.setattr("kew.audit.open_managed_store", _record)
    monkeypatch.setattr("kew.audit.project_root", lambda: project)
    monkeypatch.delenv("KEW_LOG_DIR", raising=False)
    monkeypatch.setenv("KEW_PROJECT", str(project))

    anchors = tmp_path / "workspace" / "audit-anchors.jsonl"
    anchors.parent.mkdir()
    result = CliRunner().invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert result.exit_code == 0, result.output
    assert calls and all(call["read_only"] is True for call in calls)
    assert calls[0]["db_path"] == db
    assert anchors.read_text().strip()


CI_WF = WF.parent / "ci.yml"


def _preflight_step(workflow: Path, job: str) -> dict:
    """The shared-clone heal step of a job, by name."""
    data = yaml.safe_load(workflow.read_text())
    steps = data["jobs"][job]["steps"]
    return next(s for s in steps if str(s.get("name", "")).startswith("Preflight"))


def test_preflight_is_byte_identical_to_the_ci_preflight():
    """Task 78: the anchor job runs the *same* preamble ci.yml uses.

    Two hand-maintained copies of a shared-clone heal drift, and the drift is
    invisible until the anchor job dies on a pruned checkout — the failure mode
    that left the chain unwitnessed from 2026-07-30. Pin them equal instead.
    """
    # The lint job moved to hosted Ubuntu in the staged migration (SFD-222);
    # the self-hosted test job still runs the shared-clone heal preamble.
    assert _preflight_step(WF, "anchor")["run"] == _preflight_step(CI_WF, "test")["run"]
    assert _preflight_step(WF, "anchor")["shell"] == "bash"


def _push_step():
    data = yaml.safe_load(WF.read_text())
    steps = data["jobs"]["anchor"]["steps"]
    return next(
        s for s in steps if str(s.get("name", "")).startswith("Commit and push")
    )


def test_push_step_rebases_onto_the_live_tip_before_pushing():
    # Scheduled runs can wait hours in the runner queue; a bare `git push`
    # from the checked-out github.sha was then rejected non-fast-forward on
    # every scheduled run after 2026-08-25 (#1408). The step must fetch and
    # rebase first, push the rebased HEAD to main, and bound its retries.
    run = str(_push_step()["run"])
    fetch = run.index("git fetch origin main")
    rebase = run.index("git rebase origin/main")
    push = run.index("git push origin HEAD:main")
    assert fetch < rebase < push
    assert "git push\n" not in run and not run.rstrip().endswith("git push")
    # The fetch is inside the retry loop, so a transient fetch failure retries.
    assert run.index("for attempt in 1 2 3") < fetch
    assert "git rebase --abort" in run


def test_the_anchor_runs_only_on_the_self_hosted_runner_of_a_private_repo():
    """No hosted anchor survives here, disabled or otherwise: the canonical
    ledger exists only on the self-hosted machine, so a hosted fallback would
    anchor an empty chain and report success. Outside a private repository the
    anchor job is skipped, and its runner still resolves to a hosted one. The
    only other job reads the repository's visibility and runs no repo code."""
    data = yaml.safe_load(WF.read_text())
    assert set(data["jobs"]) == {"visibility", "anchor"}
    anchor = data["jobs"]["anchor"]
    assert anchor["if"] == "needs.visibility.outputs.visibility == 'private'"
    assert resolve_runner(anchor, "private") == {SELF_HOSTED}
    for visibility in NON_PRIVATE_VISIBILITIES:
        assert resolve_runner(anchor, visibility) == {HOSTED_RUNNER}
    resolver = data["jobs"]["visibility"]
    assert resolver["runs-on"] == HOSTED_RUNNER
    assert not any("uses" in step for step in resolver["steps"])


def test_the_anchor_workflow_is_never_pull_request_triggered():
    """ci.yml runs PR-authored code, so its self-hosted jobs exist only in a
    private repository. This workflow never runs PR-authored code for exactly
    one reason — it has no `pull_request` trigger — so its runner choice is
    only safe while that stays true."""
    data = yaml.safe_load(WF.read_text())
    triggers = data[True] if True in data else data["on"]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
