"""Workflows write to GitHub only while the repository is private.

The public repository is a snapshot of the private one, workflows included.
Left alone, the scheduled workflows would push branches, open pull requests
and file issues (about secrets, surviving mutants, rule drift, ``ready`` work
for an agent) in public. So every step in EVERY workflow that writes to the
repository or its tracker must run only in a private repository: a top-level
``== 'private'`` conjunct in the step's ``if:`` or its job's ``if:``. A
scheduled event carries no repository object, so those workflows read the
visibility from the API in a ``visibility`` job.

The few writes meant to run in public are named in ``PUBLIC_WRITES``, each
with its reason.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.policy

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
READ_VISIBILITY = 'gh api "repos/${GITHUB_REPOSITORY}" --jq .visibility'
_PRIVATE_CONJUNCTS = {
    "needs.visibility.outputs.visibility == 'private'",
    "github.event.repository.visibility == 'private'",
}

# Writes that run in public on purpose: (workflow, job, step) -> reason.
PUBLIC_WRITES = {
    ("release.yml", "tag-release", "Create the tag and the GitHub release"): (
        "the release is cut in the public repository: its tag and GitHub release"
    ),
}

_GH_WRITE = re.compile(
    r"\bgh\s+(?:"
    r"issue\s+(?:create|edit|comment|close|reopen|delete|lock|transfer)"
    r"|pr\s+(?:create|edit|comment|merge|close|reopen|ready|review)"
    r"|workflow\s+(?:run|enable|disable)"
    r"|repo\s+(?:edit|create|delete|rename|archive|sync|deploy-key)"
    r"|(?:secret|variable)\s+(?:set|delete)"
    r"|run\s+(?:rerun|cancel|delete)"
    r"|cache\s+delete"
    r"|release\b|label\b)"
)
_GH_API = re.compile(r"\bgh\s+api\b(?P<rest>.*)")
_GH_API_WRITE = re.compile(
    r"(?:-X|--method)[\s=]*['\"]?(?:POST|PUT|PATCH|DELETE)\b"
    r"|\s(?:-f|-F|--field|--raw-field|--input)(?=[\s=])"
)
_CURL_WRITE = re.compile(
    r"(?:-X|--request)[\s=]*['\"]?(?:POST|PUT|PATCH|DELETE)\b"
    r"|\s(?:-d|--data(?:-\w+)?|-F|--form|-T|--upload-file)(?=[\s=])"
)
_GIT_PUSH = re.compile(r"\bgit\b[^\n]*\bpush\b")
_REST_CALL = re.compile(r"\bgithub\.rest\.\w+\.(\w+)\s*\(")
_REST_WRITE = re.compile(
    r"^(?:create|update|add|remove|delete|set|merge|dispatch|upload|lock|unlock"
    r"|request|reRun|rerun|cancel|approve|review)"
)
_REQUEST_WRITE = re.compile(r"\bgithub\.request\(\s*['\"`](?:POST|PUT|PATCH|DELETE)\b")
_GRAPHQL_MUTATION = re.compile(
    r"\bgithub\.graphql\b[\s\S]*\bmutation\b|\bmutation\b[\s\S]*\bgithub\.graphql\b"
)
# Third-party actions whose job is to write to the repository or its tracker.
_WRITING_ACTIONS = (
    "create-pull-request",
    "comment",
    "labeler",
    "gh-pages",
    "gh-release",
    "auto-commit",
    "git-push",
    "push-action",
)


def _write_calls(text: str) -> list[str]:
    """Every write a step's shell or github-script text makes."""
    joined = text.replace("\\\n", " ")
    found = [m.group(0) for m in _GH_WRITE.finditer(joined)]
    for line in joined.splitlines():
        api = _GH_API.search(line)
        if api and _GH_API_WRITE.search(api.group("rest")):
            found.append(line.strip())
        if _GIT_PUSH.search(line):
            found.append(line.strip())
        curl = re.search(r"\bcurl\b", line) and "api.github.com" in line
        if curl and _CURL_WRITE.search(line):
            found.append(line.strip())
    found += [
        f"github.rest.*.{m.group(1)}"
        for m in _REST_CALL.finditer(joined)
        if _REST_WRITE.match(m.group(1))
    ]
    found += [m.group(0) for m in _REQUEST_WRITE.finditer(joined)]
    if _GRAPHQL_MUTATION.search(joined):
        found.append("github.graphql mutation")
    return found


def _writing_action(uses: str) -> bool:
    name = uses.split("@", 1)[0].lower()
    return any(marker in name for marker in _WRITING_ACTIONS)


def _conjuncts(condition: Any) -> set[str]:
    """The top-level `&&` parts of an expression, whitespace collapsed."""
    text = " ".join(str(condition or "").split())
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if depth == 0 and text.startswith("&&", index):
            parts.append(text[start:index])
            start = index + 2
    parts.append(text[start:])
    return {part.strip() for part in parts if part.strip()}


def _private_only(job: dict[str, Any], step: dict[str, Any]) -> bool:
    gates = _conjuncts(step.get("if")) | _conjuncts(job.get("if"))
    return bool(gates & _PRIVATE_CONJUNCTS)


def _workflows() -> dict[str, dict[str, Any]]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }


def _writes(name: str, data: dict[str, Any]) -> dict[tuple[str, str, str], list[str]]:
    found = {}
    for job_name, job in (data.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            text = str(step.get("run", "")) + "\n"
            text += str((step.get("with") or {}).get("script", ""))
            calls = _write_calls(text)
            if _writing_action(str(step.get("uses", ""))):
                calls.append(str(step["uses"]))
            if calls:
                found[(name, job_name, step.get("name") or f"step {index}")] = calls
    return found


def _ungated_writes(workflows: dict[str, dict[str, Any]]) -> list[str]:
    offenders = []
    for name, data in workflows.items():
        for key, calls in _writes(name, data).items():
            _, job_name, step_name = key
            job = data["jobs"][job_name]
            step = next(
                s
                for i, s in enumerate(job["steps"])
                if (s.get("name") or f"step {i}") == step_name
            )
            if key not in PUBLIC_WRITES and not _private_only(job, step):
                offenders.append(f"{name}: {job_name}/{step_name}: {calls}")
    return offenders


def test_the_scan_reads_every_workflow_and_finds_the_known_writes() -> None:
    workflows = _workflows()
    assert {"ci.yml", "release.yml", "sast-rule-drift.yml"} <= set(workflows)
    found = {key for name, data in workflows.items() for key in _writes(name, data)}
    for known in (
        ("pricing-freshness.yml", "catalog-refresh", "Push the refresh branch"),
        ("pricing-freshness.yml", "catalog-refresh", "Open or update the refresh PR"),
        ("pricing-freshness.yml", "pricing-drift", "File ready-labeled issue on drift"),
        ("scheduled-audits.yml", "gitleaks-history", "Create issue on failure"),
        ("sast-rule-drift.yml", "rule-drift", "File the drift issue"),
        ("approval.yml", "decide", "Acknowledge on the issue"),
        (
            "audit-anchor.yml",
            "anchor",
            "Commit and push the anchor (no-op when unchanged)",
        ),
        *PUBLIC_WRITES,
    ):
        assert known in found, known


def test_every_write_runs_only_in_a_private_repository() -> None:
    offenders = _ungated_writes(_workflows())
    assert offenders == [], "\n".join(offenders)


def test_every_public_write_exemption_is_still_used() -> None:
    workflows = _workflows()
    found = {key for name, data in workflows.items() for key in _writes(name, data)}
    assert set(PUBLIC_WRITES) <= found, "drop stale PUBLIC_WRITES entries"


@pytest.mark.parametrize(
    "name", ["pricing-freshness.yml", "scheduled-audits.yml", "sast-rule-drift.yml"]
)
def test_scheduled_workflows_read_visibility_from_the_api(name: str) -> None:
    visibility = _workflows()[name]["jobs"]["visibility"]
    assert visibility["permissions"] == {"contents": "read"}
    assert visibility["outputs"] == {
        "visibility": "${{ steps.read.outputs.visibility }}"
    }
    (read,) = visibility["steps"]
    assert READ_VISIBILITY in read["run"]
    assert "uses" not in read


@pytest.mark.parametrize(
    "text",
    [
        "git push origin HEAD:main",
        'git -c http.extraheader="x" \\\n  push --force origin HEAD',
        'gh issue create --title "t"',
        "gh issue comment 1 --body b",
        "gh issue close 1",
        "gh pr create --fill",
        "gh pr merge 2 --squash",
        "gh release upload v1 dist/*",
        "gh label create ready",
        "gh api -X POST repos/o/r/issues",
        "gh api --method=DELETE repos/o/r/git/refs/tags/v1",
        "gh api repos/o/r/issues -f title=t",
        "gh api repos/o/r/labels -F name=x",
        "await github.rest.issues.create({})",
        "await github.rest.pulls.update({})",
        "await github.rest.issues.addLabels({})",
        "await github.rest.git.deleteRef({})",
        "await github.rest.actions.createWorkflowDispatch({})",
        "await github.rest.repos.merge({})",
        "gh workflow run release.yml -f release_version=1.0.0",
        "gh repo edit --visibility public",
        "gh secret set TOKEN --body x",
        "gh variable set PUBLISH_RELEASE --body true",
        "curl -X POST https://api.github.com/repos/o/r/issues",
        "curl -sS --request DELETE https://api.github.com/repos/o/r/git/refs/tags/v1",
        'curl -d \'{"title":"t"}\' https://api.github.com/repos/o/r/issues',
        "await github.request('POST /repos/{owner}/{repo}/issues', {})",
        'await github.request("DELETE /repos/o/r/git/refs/tags/v1")',
        "await github.graphql(`mutation { addStar(input: {}) { clientMutationId } }`)",
        "await github.rest.repos.uploadReleaseAsset({})",
        "await github.rest.issues.lock({})",
        "await github.rest.pulls.requestReviewers({})",
        "await github.rest.actions.reRunWorkflow({})",
    ],
)
def test_each_way_to_write_is_recognised(text: str) -> None:
    assert _write_calls(text), text


@pytest.mark.parametrize(
    "text",
    [
        'gh api "repos/${GITHUB_REPOSITORY}" --jq .visibility',
        "gh issue view 1",
        "gh pr view 2 --json state",
        "git fetch origin main",
        "await github.rest.issues.listForRepo({})",
        "await github.rest.pulls.get({})",
    ],
)
def test_a_read_is_not_a_write(text: str) -> None:
    assert _write_calls(text) == [], text


@pytest.mark.parametrize(
    "uses",
    [
        "peter-evans/create-pull-request@0123456789abcdef0123456789abcdef01234567",
        "peter-evans/create-or-update-comment@v4",
        "actions/labeler@v5",
        "peaceiris/actions-gh-pages@v4",
        "softprops/action-gh-release@v2",
        "stefanzweifel/git-auto-commit-action@v5",
    ],
)
def test_an_action_that_writes_is_recognised(uses: str) -> None:
    assert _writing_action(uses), uses


@pytest.mark.parametrize(
    "uses",
    [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/download-artifact@v8",
        "actions/github-script@v9",
        "astral-sh/setup-uv@v10",
    ],
)
def test_an_action_that_reads_is_not_a_write(uses: str) -> None:
    assert not _writing_action(uses), uses


def _duplicate_step_names(workflows: dict[str, dict[str, Any]]) -> list[str]:
    duplicates = []
    for name, data in workflows.items():
        for job_name, job in (data.get("jobs") or {}).items():
            names = [s["name"] for s in job.get("steps") or [] if s.get("name")]
            duplicates += [
                f"{name}: {job_name}/{step}"
                for step in sorted(set(names))
                if names.count(step) > 1
            ]
    return duplicates


def test_no_two_steps_in_a_job_share_a_name() -> None:
    """PUBLIC_WRITES is keyed by (workflow, job, step name): a second step of
    the same name would ride on the first one's exemption."""
    assert _duplicate_step_names(_workflows()) == []


def test_a_shared_step_name_is_caught() -> None:
    workflows = deepcopy(_workflows())
    steps = workflows["release.yml"]["jobs"]["tag-release"]["steps"]
    steps.append({"name": steps[-1]["name"], "run": "gh release delete v1 -y"})
    assert _duplicate_step_names(workflows) != []


def _ungate_sast(workflows: dict[str, dict[str, Any]]) -> None:
    for step in workflows["sast-rule-drift.yml"]["jobs"]["rule-drift"]["steps"]:
        if step.get("name") == "File the drift issue":
            step["if"] = "steps.diff.outputs.drift == 'true'"


def _ungate_with_an_or(workflows: dict[str, dict[str, Any]]) -> None:
    steps = workflows["pricing-freshness.yml"]["jobs"]["pricing-drift"]["steps"]
    for step in steps:
        if step.get("name") == "File ready-labeled issue on drift":
            step["if"] = (
                "steps.audit.outputs.drift == 'true' || "
                "needs.visibility.outputs.visibility == 'private'"
            )


def _a_new_public_comment(workflows: dict[str, dict[str, Any]]) -> None:
    workflows["ci.yml"]["jobs"]["lint"]["steps"].append(
        {"name": "Comment", "run": 'gh pr comment "$PR" --body hi'}
    )


def _ungate_a_job(workflows: dict[str, dict[str, Any]]) -> None:
    workflows["audit-anchor.yml"]["jobs"]["anchor"].pop("if")


@pytest.mark.parametrize(
    "mutate", [_ungate_sast, _ungate_with_an_or, _a_new_public_comment, _ungate_a_job]
)
def test_an_ungated_write_fails_the_check(mutate: Any) -> None:
    workflows = deepcopy(_workflows())
    mutate(workflows)
    assert _ungated_writes(workflows) != []
