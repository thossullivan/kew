"""Guard: no workflow expands a `${{ ... }}` expression inside a shell script.

`${{ ... }}` is substituted into a `run:` script before the shell parses it,
so a branch name, PR title, comment body, dispatch input, or step output
becomes shell code (#2022). Values must reach the shell through `env:` as a
quoted variable, the pattern GitHub recommends. The guard rejects every
expression in a `run:` body, not only event fields, so reverting any env:
indirection turns it red. Concurrency groups, `if:` and `env:` are not shell
and may use expressions directly.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTION_TEMPLATE = REPO_ROOT / "create-kew" / "templates" / "kew-action.yml"
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))

_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)

# Pre-existing run: expressions this guard tolerates. Each is an exact
# (file name, expression) pair whose value no outsider can choose. Anything
# new goes through env:, not onto this list.
_ALLOWED: dict[tuple[str, str], str] = {
    ("ci.yml", "${{ matrix.python-version }}"): "literal matrix value in ci.yml",
    ("ci.yml", "${{ job.status }}"): "runner-set enum: success/failure/cancelled",
    ("release.yml", "${{ job.status }}"): "runner-set enum: success/failure/cancelled",
}


def _run_blocks(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            if isinstance(step, dict) and "run" in step:
                label = step.get("name") or f"step {index}"
                blocks.append((f"{job_name}/{label}", str(step["run"])))
    return blocks


def _expressions(path: Path) -> list[tuple[str, str]]:
    """Every (step, expression) in a run: body, single-line or block scalar."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        (where, match.group(0))
        for where, script in _run_blocks(workflow)
        for match in _EXPRESSION.finditer(script)
    ]


def _injections(path: Path) -> list[str]:
    return [
        f"{path.name}: {where}: {expression}"
        for where, expression in _expressions(path)
        if (path.name, expression) not in _ALLOWED
    ]


def test_the_guard_sees_the_template_and_the_repository_workflows() -> None:
    assert ACTION_TEMPLATE.is_file()
    assert {"ci.yml", "approval.yml", "release.yml"} <= {p.name for p in WORKFLOWS}
    assert _run_blocks(yaml.safe_load(ACTION_TEMPLATE.read_text()))


@pytest.mark.parametrize(
    "path", [ACTION_TEMPLATE, *WORKFLOWS], ids=lambda path: path.name
)
def test_no_run_block_expands_an_expression(path: Path) -> None:
    offenders = _injections(path)
    assert not offenders, "move these values into env: and quote them:\n" + (
        "\n".join(offenders)
    )


def test_every_allowlist_entry_is_still_used() -> None:
    seen = {
        (path.name, expression)
        for path in (ACTION_TEMPLATE, *WORKFLOWS)
        for _, expression in _expressions(path)
    }
    assert not set(_ALLOWED) - seen, "drop stale allowlist entries"


_SINGLE_LINE = """\
on: workflow_dispatch
jobs:
  j:
    runs-on: x
    steps:
      - run: kew dispatch {expr}
"""

_BLOCK_SCALAR = """\
on: workflow_dispatch
jobs:
  j:
    runs-on: x
    steps:
      - id: s
        run: |
          echo start
          kew ci-fix "{expr}"
"""


@pytest.mark.parametrize("template", [_SINGLE_LINE, _BLOCK_SCALAR])
@pytest.mark.parametrize(
    "expr",
    [
        "${{ inputs.issue_number }}",
        "${{ steps.x.outputs.y }}",
        "${{ steps.find-run.outputs.run_id }}",
        "${{ github.event.check_suite.head_branch }}",
        "${{github.head_ref}}",
        "${{ github.event['pull_request']['title'] }}",
        "${{ format('{0}', github.event.comment.body) }}",
    ],
)
def test_the_guard_rejects_any_run_expression(
    tmp_path: Path, template: str, expr: str
) -> None:
    # The allowlist is keyed on the real file name; a synthetic ci.yml or
    # release.yml must still fail for expressions not on it.
    for name in ("bad.yml", "ci.yml", "release.yml"):
        path = tmp_path / name
        path.write_text(template.replace("{expr}", expr))
        assert _injections(path) == [f"{name}: j/step 0: {expr}"]


def test_the_guard_allows_env_indirection(tmp_path: Path) -> None:
    workflow = """\
on: push
concurrency:
  group: g-${{ github.head_ref }}
jobs:
  j:
    runs-on: x
    steps:
      - if: github.event.label.name == 'ready'
        env:
          BRANCH: ${{ github.event.check_suite.head_branch }}
          ISSUE_NUMBER: ${{ inputs.issue_number }}
        run: |
          kew dispatch "$ISSUE_NUMBER"
          echo "$BRANCH"
"""
    path = tmp_path / "good.yml"
    path.write_text(workflow)
    assert _injections(path) == []
