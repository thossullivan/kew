"""Round 18 regressions; each test asserts behavior absent at b7a8a6c5."""

import json
from contextlib import ExitStack, nullcontext
from dataclasses import is_dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_w42_round17 import (
    _admitted_execution,
    _config,
    _preflight_environment,
    _prepare_direct_command,
    _record,
    _reply,
    report_command,
)
from typer.testing import CliRunner

import kew.cli as cli
from kew.graph import plan_run
from kew.run_store import SqliteRunStore
from kew.sandbox import SandboxRuntimeUnavailable
from kew.tracking import DispatchResult

WARNING = "Outcome data is stale: 1 GitHub queries failed; PR states may be out of date"
REPO_MESSAGE = "Report repository unavailable. Set project.repo in kew.toml."


@pytest.mark.parametrize("query_code,expected_exit", [(0, 0), (1, 2)])
def test_round18b_status_skips_unconfigured_orphan_query(
    tmp_path, monkeypatch, query_code, expected_exit
):
    config = _config(tmp_path, monkeypatch)
    config.project.repo = ""
    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli._preflight"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch(
            "kew.cli.run_with_backoff", return_value=_reply("0", code=query_code)
        ) as counts,
        patch("kew.cli.detect_orphan_prs") as orphans,
    ):
        result = CliRunner().invoke(cli.app, ["status", "--history"])

    assert result.exit_code == expected_exit, result.output
    orphans.assert_not_called()
    assert counts.call_count == (5 if query_code == 0 else 1)
    if query_code == 0:
        assert "Workers configured:" in result.output
        assert "Cost:" in result.output
    else:
        assert "GitHub query failed" in result.output


def _ci_fix_scene(stack, tmp_path, monkeypatch, store):
    config = _config(tmp_path, monkeypatch)
    config.parallel.max_workers = 1
    _prepare_direct_command(stack, tmp_path, "ci-fix", config)
    _preflight_environment(stack)
    stack.enter_context(patch("kew.cli.load_config", return_value=config))
    stack.enter_context(patch("kew.cli.run", return_value=_reply()))
    stack.enter_context(
        patch("kew.cli._open_run_store", return_value=nullcontext(store))
    )
    return config


def test_round18_cap_refusal_never_mutates_labels(tmp_path, monkeypatch):
    """Base fails: update_pr_labels runs despite refused atomic admission."""
    remote_labels = {"agent-pr"}
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        store.save(_record("unrelated"))
        labels = stack.enter_context(
            patch(
                "kew.ci_fix.update_pr_labels",
                side_effect=lambda *a, **k: remote_labels.update(k["add"]) or True,
            )
        )
        launch = stack.enter_context(patch("kew.dispatch_core._run_retry_loop"))
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 2, result.output
        labels.assert_not_called()
        assert not any(label.startswith("ci-fix-attempt-") for label in remote_labels)
        launch.assert_not_called()
        assert [row.run_id for row in store.list_pending()] == ["unrelated"]


def test_round18_admitted_labels_precede_runner(tmp_path, monkeypatch):
    """Base fails: call order starts with labels, before admission."""
    order = []
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        admit = store.admit_run_with_identity
        execute = _admitted_execution(store, [])

        def admitted(*args, **kwargs):
            result = admit(*args, **kwargs)
            order.append("admitted")
            return result

        def label(*args, **kwargs):
            order.append("labels")
            return True

        def runner(*args, **kwargs):
            order.append("runner")
            return execute(*args, **kwargs)

        stack.enter_context(
            patch.object(store, "admit_run_with_identity", side_effect=admitted)
        )
        labels = stack.enter_context(
            patch("kew.ci_fix.update_pr_labels", side_effect=label)
        )
        stack.enter_context(
            patch("kew.dispatch_core._run_retry_loop", side_effect=runner)
        )
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 1, result.output
        assert order == ["admitted", "labels", "runner"]
        labels.assert_called_once()
        assert labels.call_args.kwargs == {"add": ["ci-fix-attempt-1"], "remove": None}


@pytest.mark.parametrize("failure", [False, RuntimeError("label service failed")])
def test_round18_label_failure_releases_admission(tmp_path, monkeypatch, failure):
    """Base fails: no refused row exists, or a false label result launches."""
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        labels = stack.enter_context(patch("kew.ci_fix.update_pr_labels"))
        if isinstance(failure, Exception):
            labels.side_effect = failure
        else:
            labels.return_value = failure
        launch = stack.enter_context(patch("kew.dispatch_core._run_retry_loop"))
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 2, result.output
        rows = store.list_all()
        assert len(rows) == 1, "Hook failure must finalize the admitted row."
        assert rows[0].status == "refused"
        assert rows[0].error_category == "admission_hook_refused"
        assert rows[0].finished_at is not None
        assert rows[0].estimated_cost_usd == 0
        assert store.list_pending() == []
        launch.assert_not_called()
        labels.assert_called_once()
        assert store.try_admit_run(_record("next"), cap=1)


def unresolved_report(tmp_path, monkeypatch, *, mode, reply):
    config = _config(tmp_path, monkeypatch)
    config.project.repo = ""
    target = tmp_path / "report.md"
    flags = {
        "file": ["-o", str(target)],
        "notify": ["--notify"],
        "models": ["--models", "-o", str(target)],
        "export": ["export", "--output", str(target), "--format", "json"],
    }
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.run", return_value=reply),
        patch("kew.cli.deliver_report") as webhook,
        patch("kew.cli.ingest_cap_alert", return_value=None),
        patch("kew.cli.build_report_result", wraps=cli.build_report_result) as report,
        patch(
            "kew.cli.build_model_scorecard_result",
            wraps=cli.build_model_scorecard_result,
        ) as scorecard,
    ):
        result = CliRunner().invoke(cli.app, ["report", *flags[mode]])
    return result, target, webhook, report, scorecard


@pytest.mark.parametrize("mode", ["file", "notify", "models", "export"])
@pytest.mark.parametrize("code,stdout", [(1, ""), (0, " \n")])
def test_round18_report_requires_repo(tmp_path, monkeypatch, mode, code, stdout):
    """Base fails: unresolved repositories render and lack the static diagnostic."""
    result, target, webhook, report, scorecard = unresolved_report(
        tmp_path, monkeypatch, mode=mode, reply=_reply(stdout, code=code)
    )
    assert result.exit_code == 2, result.output
    assert REPO_MESSAGE in result.stderr
    assert not target.exists()
    webhook.assert_not_called()
    report.assert_not_called()
    scorecard.assert_not_called()


def plan_command(tmp_path, monkeypatch, mode):
    config = _config(tmp_path, monkeypatch)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan")
    checkout = tmp_path / "checkout"
    manifest = checkout / plan_run.MANIFEST_RELPATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("not JSON")
    containment = SimpleNamespace(sandboxed=False, image=None, posture="native")
    result = DispatchResult(0, 0, tmp_path / "run.log", launched=True)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(plan_run, "_epic_precondition", return_value=[])
        )
        stack.enter_context(
            patch.object(plan_run, "_setup_plan_worktree", return_value=checkout)
        )
        stack.enter_context(
            patch.object(plan_run, "_copy_plan_into", return_value="plan.md")
        )
        contain = stack.enter_context(
            patch.object(plan_run, "resolve_flow_containment", return_value=containment)
        )
        preflight = stack.enter_context(
            patch("kew.runner_preflight.runner_preflight_failure", return_value=None)
        )
        launch = stack.enter_context(
            patch("kew.dispatch_core.run_tracked_agent", return_value=result)
        )
        if mode == "containment":
            contain.side_effect = SandboxRuntimeUnavailable("runtime unavailable")
        elif mode == "preflight":
            preflight.return_value = "runner unavailable"
        elif mode == "checkout":
            stack.enter_context(
                patch.object(
                    plan_run,
                    "_setup_plan_worktree",
                    side_effect=OSError("clone failed"),
                )
            )
        elif mode == "export":
            stack.enter_context(
                patch.object(
                    plan_run,
                    "_export_plan_manifest",
                    side_effect=RuntimeError("export failed"),
                )
            )
        elif mode == "startup":
            result.launched = False
            result.error_category = "sandbox_startup_refused"
        outcomes = []
        dispatch = plan_run._dispatch_plan_with_resplit

        def observed(**kwargs):
            outcome = dispatch(**kwargs)
            outcomes.append(outcome)
            return outcome

        stack.enter_context(
            patch.object(plan_run, "_dispatch_plan_with_resplit", side_effect=observed)
        )
        code = plan_run.run_epic_plan(
            name="demo",
            config=config,
            plan_path=plan,
            manifest_path=None,
            dry_run=True,
            store=None,
        )
    return code, outcomes[0], launch


@pytest.mark.parametrize(
    "mode", ["containment", "preflight", "checkout", "startup", "manifest", "export"]
)
def test_round18_plan_preserves_launch_outcome(tmp_path, monkeypatch, mode):
    """Base fails: dispatch returns None/Path and loses availability evidence."""
    code, outcome, launch = plan_command(tmp_path, monkeypatch, mode)
    assert is_dataclass(outcome), (
        "Planning must return a typed outcome, never None/Path."
    )
    refused = mode in {"containment", "preflight", "checkout", "startup"}
    assert code == (2 if refused else 1)
    assert outcome.could_not_run is refused
    assert outcome.launched is (not refused)
    if mode in {"containment", "preflight", "checkout"}:
        launch.assert_not_called()
    else:
        launch.assert_called_once()


def post_creation_command(tmp_path, monkeypatch):
    from test_epic_plan_run import _issue, _write_manifest

    from kew.graph.plan import CreatedIssue

    config = _config(tmp_path, monkeypatch)
    manifest = _write_manifest(tmp_path, [_issue("a")])
    with (
        patch.object(plan_run, "_epic_precondition", return_value=[]),
        patch.object(plan_run, "ensure_labels"),
        patch.object(
            plan_run,
            "create_issues",
            return_value=[CreatedIssue("a", 101, "title", "url")],
        ),
        patch.object(plan_run, "link_native_relations", return_value=[]),
        patch.object(plan_run, "_warn_if_plan_uncommitted"),
        patch.object(
            plan_run, "build_graph", side_effect=RuntimeError("GitHub unavailable")
        ) as read,
        patch.object(plan_run.time, "sleep"),
    ):
        code = plan_run.run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
    assert read.call_count == 3
    return code


def test_round18_post_creation_read_failure(tmp_path, monkeypatch):
    """Base fails: exhausted GitHub reads return 1."""
    assert post_creation_command(tmp_path, monkeypatch) == 2


@pytest.mark.parametrize("mode", ["file", "notify", "models"])
def test_round18_report_warning_precedes_delivery(tmp_path, monkeypatch, mode):
    """Base fails: delivered content lacks the staleness warning."""
    result, content = report_command(tmp_path, monkeypatch, mode=mode)
    assert result.exit_code == 2, result.output
    assert content.startswith(f"# {WARNING}\n\n")
    healthy, content = report_command(tmp_path, monkeypatch, mode=mode, failures=0)
    assert healthy.exit_code == 0, healthy.output
    assert "Outcome data is stale" not in content


@pytest.mark.parametrize("fmt", ["json", "html"])
def test_round18_export_availability_metadata(tmp_path, monkeypatch, fmt):
    """Base fails: exports discard synchronization failures and return 0."""
    config = _config(tmp_path, monkeypatch)
    target = tmp_path / f"report.{fmt}"
    db_path = tmp_path / "logs" / "dispatch.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.touch()
    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch("kew.report_export._open_report_store", return_value=nullcontext(store)),
        patch("kew.outcomes.run", return_value=_reply(code=1)),
    ):
        row = _record("stale", item="42", status="success")
        row.pr_url = "https://github.com/o/r/pull/42"
        row.pr_state = "OPEN"
        store.save(row)
        result = CliRunner().invoke(
            cli.app, ["report", "export", "--output", str(target), "--format", fmt]
        )
    assert result.exit_code == 2, result.output
    content = target.read_text()
    if fmt == "html":
        assert f'<h1 role="alert">{WARNING}</h1>' in content
        content = content.split('id="kew-evidence-data">')[1].split("</script>")[0]
    assert json.loads(content)["outcome_sync"] == {"query_failures": 1, "stale": True}
