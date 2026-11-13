"""The weekly model-catalog refresh PR (#2718): its workflow job and body builder.

The workflow half parses ``.github/workflows/pricing-freshness.yml`` and pins
what the job may do: never pass ``--accept-large-change``, hold exactly the
three write permissions it needs, push only ``models-refresh/weekly``, and use
only SHA-pinned actions. Each check also runs against a mutated copy, so a
check that cannot fail would show up here.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from kew.model_refresh import Change, RefreshResult, render_report

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_refresh_pr.py"
_spec = importlib.util.spec_from_file_location("model_refresh_pr", SCRIPT)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
sys.modules["model_refresh_pr"] = pr
_spec.loader.exec_module(pr)

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "pricing-freshness.yml"
)
REFRESH_JOB = "catalog-refresh"
PUSH_REFSPEC = "HEAD:refs/heads/models-refresh/weekly"
_SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_LABELS = list(pr.REQUIRED_LABELS)


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in data["jobs"].values() for step in job["steps"]]


def _commands(run: str) -> list[list[str]]:
    """Shell commands of a run block, continuation lines joined and split."""
    joined = run.replace("\\\n", " ")
    return [shlex.split(line, comments=True) for line in joined.splitlines()]


# --- workflow checks -------------------------------------------------------


def _check_refresh_never_accepts_large_change(data: dict[str, Any]) -> None:
    runs = [step.get("run", "") for step in _steps(data)]
    assert any("kew models refresh" in run for run in runs), "no refresh step"
    for run in runs:
        assert "--accept-large-change" not in run, run


def _check_permissions(data: dict[str, Any]) -> None:
    assert "permissions" not in data, "workflow-level permissions widen every job"
    assert data["jobs"][REFRESH_JOB]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
    }


def _git_pushes(data: dict[str, Any]) -> list[list[str]]:
    pushes = []
    for step in _steps(data):
        for argv in _commands(step.get("run", "")):
            if "git" in argv and "push" in argv[argv.index("git") :]:
                pushes.append(argv[argv.index("push") + 1 :])
    return pushes


def _check_pushes_only_the_refresh_branch(data: dict[str, Any]) -> None:
    pushes = _git_pushes(data)
    assert pushes, "the refresh job must push its branch"
    for args in pushes:
        refs = [arg for arg in args if not arg.startswith("-")]
        assert refs == ["origin", PUSH_REFSPEC], args
        assert not {"--all", "--mirror", "--tags"} & set(args), args
    # The API is a second way to move a ref; no script step may use it.
    for step in _steps(data):
        script = step.get("with", {}).get("script", "")
        assert "createRef" not in script and "updateRef" not in script


def _check_actions_sha_pinned(data: dict[str, Any]) -> None:
    uses = [step["uses"] for step in _steps(data) if "uses" in step]
    assert uses
    for ref in uses:
        assert _SHA_PIN.match(ref), f"{ref} is not pinned to a commit SHA"


_CHECKS = (
    _check_refresh_never_accepts_large_change,
    _check_permissions,
    _check_pushes_only_the_refresh_branch,
    _check_actions_sha_pinned,
)


@pytest.mark.parametrize("check", _CHECKS, ids=lambda c: c.__name__)
def test_workflow_holds(check: Any) -> None:
    check(_workflow())


def _refresh_step(data: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in _steps(data) if "kew models refresh" in s.get("run", ""))


def _push_step(data: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in _steps(data) if "git -c" in s.get("run", ""))


def _accepts_large_change(data: dict[str, Any]) -> None:
    step = _refresh_step(data)
    step["run"] = step["run"].replace("--write", "--write --accept-large-change")


def _adds_permission(data: dict[str, Any]) -> None:
    data["jobs"][REFRESH_JOB]["permissions"]["actions"] = "write"


def _drops_permission(data: dict[str, Any]) -> None:
    del data["jobs"][REFRESH_JOB]["permissions"]["issues"]


def _pushes_main(data: dict[str, Any]) -> None:
    step = _push_step(data)
    step["run"] += "\ngit push origin HEAD:refs/heads/main\n"


def _pushes_tags_too(data: dict[str, Any]) -> None:
    step = _push_step(data)
    step["run"] = step["run"].replace("push --force", "push --force --tags")


def _unpins_an_action(data: dict[str, Any]) -> None:
    data["jobs"][REFRESH_JOB]["steps"][0]["uses"] = "actions/checkout@v7"


@pytest.mark.parametrize(
    ("mutate", "check"),
    [
        (_accepts_large_change, _check_refresh_never_accepts_large_change),
        (_adds_permission, _check_permissions),
        (_drops_permission, _check_permissions),
        (_pushes_main, _check_pushes_only_the_refresh_branch),
        (_pushes_tags_too, _check_pushes_only_the_refresh_branch),
        (_unpins_an_action, _check_actions_sha_pinned),
    ],
    ids=lambda f: f.__name__,
)
def test_each_workflow_check_fails_on_a_mutated_copy(mutate: Any, check: Any) -> None:
    data = copy.deepcopy(_workflow())
    mutate(data)
    with pytest.raises(AssertionError):
        check(data)


def test_workflow_passes_the_label_set_the_builder_requires() -> None:
    job = _workflow()["jobs"][REFRESH_JOB]
    builder = next(s for s in job["steps"] if "model_refresh_pr.py" in s.get("run", ""))
    argv = _commands(builder["run"])[0]
    passed = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--label"]
    assert passed == _LABELS
    opener = next(s for s in job["steps"] if "pulls.create" in str(s.get("with")))
    assert f"const labels = {_LABELS!r};" in opener["with"]["script"]


def test_audit_job_keeps_its_read_only_token() -> None:
    assert _workflow()["jobs"]["pricing-drift"]["permissions"] == {
        "contents": "read",
        "issues": "write",
    }


# --- PR-body builder -------------------------------------------------------

_SOURCES = ["https://openrouter.ai/api/v1/models"]


def _report(**changes: list[Change]) -> str:
    return render_report(RefreshResult(payload={}, sources=_SOURCES, **changes))


def _price_change() -> Change:
    return Change("gpt-x", "input_per_mtok", "1", "1.5", _SOURCES[0])


def test_a_catalog_with_no_change_produces_no_pr() -> None:
    assert pr.build_pr_body(_report(), _LABELS) is None


def test_disagreements_alone_are_not_a_catalog_change() -> None:
    report = _report(disagreements=[_price_change()])
    assert pr.build_pr_body(report, _LABELS) is None


@pytest.mark.parametrize("kind", ["price_changes", "lifecycle_changes"])
def test_a_change_produces_a_body_naming_the_human_merge(kind: str) -> None:
    report = _report(**{kind: [_price_change()]})
    body = pr.build_pr_body(report, _LABELS)
    assert body is not None
    assert body.startswith(report.rstrip("\n"))
    assert "`gpt-x` | `input_per_mtok` | `1` | `1.5`" in body
    assert "A person merges this PR. No automation does" in body
    assert "`model-catalog-refresh`, `needs-human-merge`" in body


@pytest.mark.parametrize("label", _LABELS)
def test_a_missing_required_label_is_refused(label: str) -> None:
    labels = [x for x in _LABELS if x != label]
    with pytest.raises(pr.ReportError, match=label):
        pr.build_pr_body(_report(price_changes=[_price_change()]), labels)


def test_a_report_without_its_change_tables_is_refused_not_read_as_no_change() -> None:
    with pytest.raises(pr.ReportError, match="Lifecycle changes"):
        pr.build_pr_body("### Price changes\n\nNone.\n", _LABELS)


def _run_main(tmp_path: Path, report: str, capsys: Any) -> tuple[int, str, Path]:
    (tmp_path / "report.md").write_text(report, encoding="utf-8")
    out = tmp_path / "body.md"
    argv = ["--report", str(tmp_path / "report.md"), "--output", str(out)]
    for label in _LABELS:
        argv += ["--label", label]
    code = pr.main(argv)
    return code, capsys.readouterr().out, out


def test_main_writes_no_body_when_nothing_changed(tmp_path: Path, capsys: Any) -> None:
    code, out, body = _run_main(tmp_path, _report(), capsys)
    assert (code, out) == (0, "changed=false\n")
    assert not body.exists()


def test_main_writes_the_body_when_the_catalog_changed(
    tmp_path: Path, capsys: Any
) -> None:
    report = _report(lifecycle_changes=[_price_change()])
    code, out, body = _run_main(tmp_path, report, capsys)
    assert (code, out) == (0, "changed=true\n")
    assert body.read_text(encoding="utf-8") == pr.build_pr_body(report, _LABELS)
