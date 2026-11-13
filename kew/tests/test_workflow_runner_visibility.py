"""No workflow job can reach the self-hosted runner unless the repo is private.

The public repository must never execute anything on the operator's machine
(re-audit F1). The real boundary is repository configuration: the public
repository registers no self-hosted runner and requires approval for fork PR
workflows (docs/operations.md, "Public repository runner rule"). A fork PR
can rewrite any workflow file, so these checks are a second line. They make
the workflow logic agree with that rule: every job's ``runs-on`` is evaluated
for every visibility, and only the exact value ``private`` may produce
``self-hosted``.

The same file pins supply-chain hygiene for the workflows (F14): every
``uses:`` names a full commit SHA, and the gitleaks download is verified
against a pinned SHA-256 before extraction.
"""

from __future__ import annotations

import copy
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from support.workflow_runners import (
    HOSTED_RUNNER,
    MISSING,
    NON_PRIVATE_VISIBILITIES,
    ROUTED_RUNNERS,
    RUNNER_BY_EVENT_VISIBILITY,
    RUNNER_BY_RESOLVED_VISIBILITY,
    SELF_HOSTED,
    WORKFLOWS_DIR,
    UnsupportedExpression,
    context_for,
    evaluate,
    resolve_runner,
    workflow_paths,
)

# Private behaviour, unchanged: exactly these jobs use the self-hosted runner
# while the repository is private.
EXPECTED_PRIVATE_SELF_HOSTED = {
    "approval.yml": {"decide"},
    "audit-anchor.yml": {"anchor"},
    "canary.yml": {"canary"},
    "ci.yml": {"test", "agent-image-scan"},
    "release.yml": {"serial-tests"},
}

# Host-state workflows act on state that exists only on the operator machine,
# so outside a private repository they are skipped rather than run hosted.
HOST_STATE_JOBS = {
    ("approval.yml", "decide"),
    ("audit-anchor.yml", "anchor"),
    ("canary.yml", "canary"),
}

RUNNER_BOUNDARY_STEP = "Runner boundary — self-hosted requires a private repository"
NOT_HOSTED = "runner.environment != 'github-hosted'"

SHA_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$", re.M)

# Copied from gitleaks_8.30.0_checksums.txt, published with the v8.30.0
# release. The pinned version is GITLEAKS_VERSION in the workflows.
GITLEAKS_SHA256 = {
    "darwin_arm64": "b251ab2bcd4cd8ba9e56ff37698c033ebf38582b477d21ebd86586d927cf87e7",
    "darwin_x64": "ca221d012d247080c2f6f61f4b7a83bffa2453806b0c195c795bbe9a8c775ed5",
    "linux_x64": "79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e",
    "linux_arm64": "b4cbbb6ddf7d1b2a603088cd03a4e3f7ce48ee7fd449b51f7de6ee2906f5fa2f",
}
GITLEAKS_INSTALLS = (
    ("ci.yml", "secrets"),
    ("scheduled-audits.yml", "gitleaks-history"),
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflows(directory: Path = WORKFLOWS_DIR) -> dict[str, dict]:
    return {path.name: _load(path) for path in workflow_paths(directory)}


def _condition(value: object) -> str:
    raw = " ".join(str(value or "").split())
    if raw.startswith("${{") and raw.endswith("}}"):
        raw = raw[3:-2].strip()
    return raw


# --- Runner selection -------------------------------------------------------


def _assert_never_self_hosted_unless_private(workflows: dict[str, dict]) -> None:
    for workflow_name, data in workflows.items():
        for job_name, job in data["jobs"].items():
            for visibility in NON_PRIVATE_VISIBILITIES:
                labels = resolve_runner(job, visibility)
                shown = "<no repository>" if visibility is MISSING else visibility
                assert labels == {HOSTED_RUNNER}, (
                    f"{workflow_name} job {job_name!r} resolves to "
                    f"{sorted(labels)} when visibility is {shown!r}. Outside a "
                    "private repository every job must resolve to "
                    f"{HOSTED_RUNNER!r}."
                )


def _private_self_hosted(workflows: dict[str, dict]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for workflow_name, data in workflows.items():
        jobs = {
            job_name
            for job_name, job in data["jobs"].items()
            if SELF_HOSTED in resolve_runner(job, "private")
        }
        if jobs:
            found[workflow_name] = jobs
    return found


def _assert_runs_on_is_literal_or_routed(workflows: dict[str, dict]) -> None:
    for workflow_name, data in workflows.items():
        for job_name, job in data["jobs"].items():
            runs_on = job.get("runs-on")
            assert runs_on == HOSTED_RUNNER or runs_on in ROUTED_RUNNERS, (
                f"{workflow_name} job {job_name!r} has runs-on {runs_on!r}. A "
                f"job names {HOSTED_RUNNER!r} literally or uses one of the two "
                "fixed visibility expressions, so the runner stays readable "
                "in review and a repository variable can never route it."
            )


def _assert_resolved_visibility_is_read_safely(workflow_name: str, data: dict) -> None:
    jobs = data["jobs"]
    users = [
        name
        for name, job in jobs.items()
        if job.get("runs-on") == RUNNER_BY_RESOLVED_VISIBILITY
    ]
    if not users:
        return
    resolver = jobs.get("visibility")
    assert resolver is not None, f"{workflow_name} has no visibility job"
    assert resolver.get("runs-on") == HOSTED_RUNNER
    assert resolver.get("permissions") == {"contents": "read"}
    steps = resolver.get("steps") or []
    assert steps and not any("uses" in step for step in steps), (
        f"{workflow_name}'s visibility job must check out nothing and run no "
        "action: it decides where repository code may run."
    )
    run = "\n".join(str(step.get("run", "")) for step in steps)
    assert 'gh api "repos/${GITHUB_REPOSITORY}" --jq .visibility' in run
    assert resolver.get("outputs") == {
        "visibility": "${{ steps.read.outputs.visibility }}"
    }
    for name in users:
        needs = jobs[name].get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "visibility" in needs, (
            f"{workflow_name} job {name!r} reads needs.visibility without "
            "needing the visibility job, so the output is always empty."
        )


def _assert_routed_jobs_open_with_the_boundary(workflows: dict[str, dict]) -> None:
    for workflow_name, data in workflows.items():
        for job_name, job in data["jobs"].items():
            source = ROUTED_RUNNERS.get(job.get("runs-on"))
            if source is None:
                continue
            steps = job.get("steps") or []
            assert steps, f"{workflow_name} job {job_name!r} has no steps"
            first = steps[0]
            assert first.get("name") == RUNNER_BOUNDARY_STEP, (
                f"{workflow_name} job {job_name!r} can use the self-hosted "
                f"runner but its first step is not {RUNNER_BOUNDARY_STEP!r}. "
                "The boundary must run before any checkout."
            )
            expected = f"{NOT_HOSTED} && {source} != 'private'"
            assert _condition(first.get("if")) == expected, (
                f"{workflow_name} job {job_name!r} boundary tests "
                f"{first.get('if')!r}, not {expected!r}."
            )
            assert "exit 1" in str(first.get("run", ""))
            assert "uses" not in first and "continue-on-error" not in first
            assert "continue-on-error" not in job


def _assert_host_state_jobs_skip_outside_private(workflows: dict[str, dict]) -> None:
    for workflow_name, job_name in HOST_STATE_JOBS:
        job = workflows[workflow_name]["jobs"][job_name]
        source = ROUTED_RUNNERS[job["runs-on"]]
        condition = _condition(job.get("if"))
        gate = f"{source} == 'private'"
        assert condition == gate or condition.startswith(f"{gate} &&"), (
            f"{workflow_name} job {job_name!r} acts on operator-host state "
            f"and must be skipped outside a private repository; its if is "
            f"{job.get('if')!r}."
        )
        for visibility in NON_PRIVATE_VISIBILITIES:
            assert evaluate(gate, context_for(visibility)) is False


def test_no_job_resolves_to_self_hosted_unless_the_repository_is_private() -> None:
    _assert_never_self_hosted_unless_private(_workflows())


def test_private_repository_keeps_its_self_hosted_jobs() -> None:
    assert _private_self_hosted(_workflows()) == EXPECTED_PRIVATE_SELF_HOSTED


def test_runs_on_is_a_literal_hosted_runner_or_a_fixed_visibility_expression() -> None:
    _assert_runs_on_is_literal_or_routed(_workflows())


def test_scheduled_workflows_read_visibility_from_a_hosted_resolver() -> None:
    for name, data in _workflows().items():
        _assert_resolved_visibility_is_read_safely(name, data)


def test_every_routed_job_opens_with_the_runner_boundary() -> None:
    _assert_routed_jobs_open_with_the_boundary(_workflows())


def test_host_state_jobs_are_skipped_outside_a_private_repository() -> None:
    _assert_host_state_jobs_skip_outside_private(_workflows())


@pytest.mark.parametrize(
    ("expression", "private", "public"),
    [
        (RUNNER_BY_EVENT_VISIBILITY, SELF_HOSTED, HOSTED_RUNNER),
        (RUNNER_BY_RESOLVED_VISIBILITY, SELF_HOSTED, HOSTED_RUNNER),
        # Fail-open spelling: an empty visibility would pick self-hosted.
        (
            "${{ github.event.repository.visibility == 'public' "
            "&& 'ubuntu-latest' || 'self-hosted' }}",
            SELF_HOSTED,
            HOSTED_RUNNER,
        ),
        ("${{ 'Private' == 'private' && 'x' || 'y' }}", "x", "x"),
        ("${{ (false || null) && 'x' || 'y' }}", "y", "y"),
        ("${{ !(true) && 'x' || 'y' }}", "y", "y"),
    ],
)
def test_expression_evaluator_table(expression: str, private: str, public: str) -> None:
    assert evaluate(expression, context_for("private")) == private
    assert evaluate(expression, context_for("public")) == public


def test_expression_evaluator_refuses_unmodelled_syntax() -> None:
    for expression in (
        "${{ fromJSON('[\"self-hosted\"]') }}",
        "${{ vars.RUNNER_TARGET }} extra",
        "${{ github.event.repository.visibility == }}",
    ):
        with pytest.raises(UnsupportedExpression):
            evaluate(expression, context_for("public"))


def _mutated(mutate) -> dict[str, dict]:
    workflows = copy.deepcopy(_workflows())
    mutate(workflows)
    return workflows


def test_runner_assertions_fail_red_for_forbidden_mutations() -> None:
    def literal_self_hosted(workflows: dict[str, dict]) -> None:
        workflows["ci.yml"]["jobs"]["test"]["runs-on"] = SELF_HOSTED

    def fail_open(workflows: dict[str, dict]) -> None:
        workflows["release.yml"]["jobs"]["serial-tests"]["runs-on"] = (
            "${{ github.event.repository.visibility == 'public' "
            "&& 'ubuntu-latest' || 'self-hosted' }}"
        )

    def self_hosted_list(workflows: dict[str, dict]) -> None:
        workflows["canary.yml"]["jobs"]["canary"]["runs-on"] = [SELF_HOSTED, "macOS"]

    for mutate in (literal_self_hosted, fail_open, self_hosted_list):
        with pytest.raises(AssertionError, match="Outside a private repository"):
            _assert_never_self_hosted_unless_private(_mutated(mutate))

    def repo_variable(workflows: dict[str, dict]) -> None:
        workflows["ci.yml"]["jobs"]["lint"]["runs-on"] = "${{ vars.RUNNER_TARGET }}"

    with pytest.raises(AssertionError, match="fixed visibility expressions"):
        _assert_runs_on_is_literal_or_routed(_mutated(repo_variable))

    def boundary_after_checkout(workflows: dict[str, dict]) -> None:
        steps = workflows["ci.yml"]["jobs"]["agent-image-scan"]["steps"]
        steps[0], steps[1] = steps[1], steps[0]

    def boundary_warns(workflows: dict[str, dict]) -> None:
        workflows["release.yml"]["jobs"]["serial-tests"]["steps"][0]["run"] = "echo"

    def boundary_wrong_source(workflows: dict[str, dict]) -> None:
        workflows["audit-anchor.yml"]["jobs"]["anchor"]["steps"][0]["if"] = (
            f"{NOT_HOSTED} && github.event.repository.visibility != 'private'"
        )

    for mutate in (boundary_after_checkout, boundary_warns, boundary_wrong_source):
        with pytest.raises(AssertionError):
            _assert_routed_jobs_open_with_the_boundary(_mutated(mutate))

    def host_job_runs_everywhere(workflows: dict[str, dict]) -> None:
        del workflows["canary.yml"]["jobs"]["canary"]["if"]

    with pytest.raises(AssertionError, match="skipped outside a private"):
        _assert_host_state_jobs_skip_outside_private(_mutated(host_job_runs_everywhere))

    def resolver_checks_out(workflows: dict[str, dict]) -> None:
        workflows["canary.yml"]["jobs"]["visibility"]["steps"].insert(
            0, {"uses": "actions/checkout@" + "0" * 40}
        )

    with pytest.raises(AssertionError, match="check out nothing"):
        _assert_resolved_visibility_is_read_safely(
            "canary.yml", _mutated(resolver_checks_out)["canary.yml"]
        )


# --- Action pinning ---------------------------------------------------------


def _uses_refs(text: str) -> list[tuple[str, str]]:
    return [
        (match.group("ref").strip("'\""), match.group("rest"))
        for match in USES_LINE.finditer(text)
    ]


def _assert_every_uses_is_sha_pinned(name: str, text: str) -> None:
    for ref, rest in _uses_refs(text):
        if ref.startswith("./") or "@" not in ref:
            # A local action runs code from this checkout, pinned by the commit.
            assert ref.startswith("./"), f"{name}: unpinned uses {ref!r}"
            continue
        assert SHA_REF.match(ref), (
            f"{name}: uses {ref!r} is not pinned to a 40-hex commit SHA. A tag "
            "or branch can be moved to different code after review."
        )
        assert re.match(r"^\s+#\s+\S+", rest), (
            f"{name}: uses {ref!r} has no trailing '# <tag>' comment naming "
            "the version the SHA was resolved from."
        )


def test_every_uses_is_pinned_to_a_full_commit_sha() -> None:
    seen = 0
    for path in workflow_paths():
        text = path.read_text(encoding="utf-8")
        _assert_every_uses_is_sha_pinned(path.name, text)
        seen += len(_uses_refs(text))
    # Parsed from YAML too, so a `uses:` the line pattern missed still counts.
    parsed = [
        step["uses"]
        for data in _workflows().values()
        for job in data["jobs"].values()
        for step in job.get("steps") or []
        if "uses" in step
    ]
    parsed += [
        job["uses"]
        for data in _workflows().values()
        for job in data["jobs"].values()
        if "uses" in job
    ]
    assert seen == len(parsed) > 0
    assert all(SHA_REF.match(ref) or ref.startswith("./") for ref in parsed)


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v7",
        "        uses: pypa/gh-action-pypi-publish@release/v1",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    ],
)
def test_pin_check_rejects_tags_short_shas_and_missing_comments(line: str) -> None:
    with pytest.raises(AssertionError):
        _assert_every_uses_is_sha_pinned("synthetic.yml", line + "\n")


# --- gitleaks checksum ------------------------------------------------------


def _install_step(workflow: str, job: str) -> dict:
    steps = _workflows()[workflow]["jobs"][job]["steps"]
    matches = [step for step in steps if step.get("name") == "Install gitleaks"]
    assert len(matches) == 1
    return matches[0]


def _assert_gitleaks_install_verifies(step: dict) -> None:
    run = str(step.get("run", ""))
    for asset, sha in GITLEAKS_SHA256.items():
        assert f'asset="{asset}";' in run and f'sha="{sha}"' in run, (
            f"gitleaks {asset} archive has no pinned SHA-256 {sha}"
        )
    assert "| tar" not in run, (
        "the gitleaks archive is piped into tar, so nothing can check it "
        "before it is extracted"
    )
    verify = run.index('if ! echo "${sha}  ${archive}" | checksum; then')
    download = run.index('curl -sSfL -o "$archive"')
    extract = run.index('tar xzf "$archive"')
    assert download < verify < extract
    assert "sha256sum -c -" in run and "shasum -a 256 -c -" in run


@pytest.mark.parametrize(("workflow", "job"), GITLEAKS_INSTALLS)
def test_gitleaks_download_is_checked_against_its_release_checksum(
    workflow: str, job: str
) -> None:
    _assert_gitleaks_install_verifies(_install_step(workflow, job))


def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.parametrize(("workflow", "job"), GITLEAKS_INSTALLS)
def test_gitleaks_install_refuses_a_substituted_archive(
    workflow: str, job: str, tmp_path: Path
) -> None:
    """Run the step's shell with a curl that returns the wrong bytes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    extracted = tmp_path / "extracted"
    # curl writes a substitute archive; tar records that it ever ran.
    _write_stub(
        bin_dir,
        "curl",
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then printf substitute > "$2"; shift; fi\n'
        "  shift\n"
        "done\n",
    )
    _write_stub(bin_dir, "tar", f'touch "{extracted}"\n')
    step = _install_step(workflow, job)
    result = subprocess.run(  # noqa: S603 - fixed argv, workflow shell under test
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],  # noqa: S607
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_PATH": str(tmp_path / "github_path"),
            **step["env"],
        },
        check=False,
    )
    assert result.returncode != 0
    assert "does not match its pinned SHA-256" in result.stdout + result.stderr
    assert not extracted.exists(), "tar ran on an archive that failed its checksum"


# --- The Switchyard image is opt-in -------------------------------------------
#
# The kew-switchyard image builds only from a pinned private source input that
# the private runner alone holds, so the tooling is opt-in: ci.yml and
# release.yml set one switch at the workflow level, to "1" only where that
# input exists. tests/test_switchyard_image_skip.py pins what the value does.

RUN_ENV = "KEW_SWITCHYARD_IMAGE"
RUN_EXPRESSION = "${{ github.event.repository.visibility == 'private' && '1' || '' }}"
RUN_WORKFLOWS = ("ci.yml", "release.yml")
# An opted-in job proves the marked tests are still selected before it runs
# them; the script no-ops when the switch is off.
ASSERT_STEP = "Preflight — the opted-in run selects the Switchyard image tests"
ASSERT_RUN = "bash .github/scripts/assert_switchyard_tests_selected.sh"
ASSERT_CONSUMERS = (
    ("ci.yml", "test", "Test (pytest)"),
    ("release.yml", "serial-tests", "Run serial tests"),
)
HOSTED_ONLY = "runner.environment == 'github-hosted'"
PROVISION_STEP = "Preflight — provision Docker inputs on the hosted runner"
PROVISION_RUN = "bash .github/scripts/provision_hosted_docker_inputs.sh"
PROVISION_SCRIPT = WORKFLOWS_DIR.parent / "scripts" / PROVISION_RUN.split("/")[-1]
PROVISIONED_CONSUMERS = (
    ("ci.yml", "test", "Test (pytest)"),
    ("release.yml", "serial-tests", "Verify serial test prerequisites"),
)


def _assert_switchyard_runs_only_in_private(workflows: dict[str, dict]) -> None:
    for workflow_name, data in workflows.items():
        top = data.get("env") or {}
        if workflow_name not in RUN_WORKFLOWS:
            assert RUN_ENV not in top, f"{workflow_name} sets {RUN_ENV}"
        else:
            expression = top.get(RUN_ENV)
            assert expression == RUN_EXPRESSION, (
                f"{workflow_name} sets {RUN_ENV} to {expression!r}, not the "
                "fixed visibility expression."
            )
            assert evaluate(expression, context_for("private")) == "1", (
                f"{workflow_name} skips the Switchyard image in a private "
                "repository, where the self-hosted runner builds it."
            )
            for visibility in NON_PRIVATE_VISIBILITIES:
                assert evaluate(expression, context_for(visibility)) == "", (
                    f"{workflow_name} opts into the Switchyard image for "
                    f"visibility {visibility!r}, where the job runs hosted and "
                    "the pinned private source input does not exist."
                )
        # One switch per workflow: no job or step may set or override it.
        for job_name, job in data["jobs"].items():
            for scope in [job, *(job.get("steps") or [])]:
                assert RUN_ENV not in (scope.get("env") or {}), (
                    f"{workflow_name} job {job_name!r} overrides {RUN_ENV}"
                )
                assert RUN_ENV not in str(scope.get("if", ""))


def test_the_switchyard_image_runs_only_in_a_private_repository() -> None:
    _assert_switchyard_runs_only_in_private(_workflows())


def test_the_switchyard_switch_assertions_fail_red_for_forbidden_mutations() -> None:
    def always(workflows: dict[str, dict]) -> None:
        workflows["ci.yml"]["env"][RUN_ENV] = "1"

    def inverted(workflows: dict[str, dict]) -> None:
        workflows["release.yml"]["env"][RUN_ENV] = (
            "${{ github.event.repository.visibility != 'private' && '1' || '' }}"
        )

    def public_only(workflows: dict[str, dict]) -> None:
        workflows["ci.yml"]["env"][RUN_ENV] = (
            "${{ github.event.repository.visibility == 'public' && '1' || '' }}"
        )

    def removed(workflows: dict[str, dict]) -> None:
        del workflows["release.yml"]["env"][RUN_ENV]

    def job_override(workflows: dict[str, dict]) -> None:
        workflows["ci.yml"]["jobs"]["agent-image-scan"]["env"] = {RUN_ENV: "1"}

    def step_override(workflows: dict[str, dict]) -> None:
        step = workflows["release.yml"]["jobs"]["serial-tests"]["steps"][-2]
        step.setdefault("env", {})[RUN_ENV] = "1"

    def elsewhere(workflows: dict[str, dict]) -> None:
        workflows["canary.yml"].setdefault("env", {})[RUN_ENV] = RUN_EXPRESSION

    for mutate in (
        always,
        inverted,
        public_only,
        removed,
        job_override,
        step_override,
        elsewhere,
    ):
        with pytest.raises(AssertionError):
            _assert_switchyard_runs_only_in_private(_mutated(mutate))


def _assert_opted_in_selection_is_asserted(workflows: dict[str, dict]) -> None:
    """A job that opts in proves the marked tests are still selected first.

    The tooling is opt-in, so the marked tests are absent on every default
    run. That makes an empty selection invisible: a drifting marker or a
    broken deselection would silently stop the sidecar packet certification
    in the one job that is supposed to run it. Each opted-in suite therefore
    runs the assertion script before the suite itself.
    """
    for workflow_name, job_name, consumer_name in ASSERT_CONSUMERS:
        steps = workflows[workflow_name]["jobs"][job_name]["steps"]
        names = [step.get("name") for step in steps]
        assert names.count(ASSERT_STEP) == 1, (
            f"{workflow_name} job {job_name!r} does not assert that an "
            "opted-in run selects the Switchyard image tests"
        )
        step = steps[names.index(ASSERT_STEP)]
        assert step.get("run") == ASSERT_RUN
        assert "continue-on-error" not in step, (
            f"{workflow_name} job {job_name!r} cannot fail on an empty "
            "Switchyard selection, so the assertion proves nothing"
        )
        assert names.index(ASSERT_STEP) < names.index(consumer_name)


def test_an_opted_in_job_asserts_the_marked_tests_are_selected() -> None:
    _assert_opted_in_selection_is_asserted(_workflows())
    script = WORKFLOWS_DIR.parent / "scripts" / ASSERT_RUN.split("/")[-1]
    text = script.read_text()
    for proof in (
        'if [ "${KEW_SWITCHYARD_IMAGE:-}" != "1" ]; then',
        'uv run pytest -m "$marker" --collect-only -q tests',
        'if [ "$selected" -lt 1 ]; then',
        "exit 1",
    ):
        assert proof in text, f"the assertion script lacks {proof!r}"


def test_the_opted_in_assertions_fail_red_for_forbidden_mutations() -> None:
    def removed(workflows: dict[str, dict]) -> None:
        steps = workflows["ci.yml"]["jobs"]["test"]["steps"]
        names = [step.get("name") for step in steps]
        steps.pop(names.index(ASSERT_STEP))

    def after_the_suite(workflows: dict[str, dict]) -> None:
        steps = workflows["release.yml"]["jobs"]["serial-tests"]["steps"]
        names = [step.get("name") for step in steps]
        steps.append(steps.pop(names.index(ASSERT_STEP)))

    def soft(workflows: dict[str, dict]) -> None:
        for step in workflows["ci.yml"]["jobs"]["test"]["steps"]:
            if step.get("name") == ASSERT_STEP:
                step["continue-on-error"] = True

    for mutate in (removed, after_the_suite, soft):
        with pytest.raises(AssertionError):
            _assert_opted_in_selection_is_asserted(_mutated(mutate))


def _assert_hosted_provisioning(workflows: dict[str, dict]) -> None:
    for workflow_name, job_name, consumer_name in PROVISIONED_CONSUMERS:
        steps = workflows[workflow_name]["jobs"][job_name]["steps"]
        names = [step.get("name") for step in steps]
        assert names.count(PROVISION_STEP) == 1, (
            f"{workflow_name} job {job_name!r} does not provision the hosted "
            "runner's Docker inputs"
        )
        provision = steps[names.index(PROVISION_STEP)]
        assert _condition(provision.get("if")) == HOSTED_ONLY, (
            f"{workflow_name} job {job_name!r} provisions outside the hosted "
            "runner; the private runner keeps these inputs as host state"
        )
        assert provision.get("run") == PROVISION_RUN
        assert "continue-on-error" not in provision
        assert names.index(PROVISION_STEP) < names.index(consumer_name)


def test_hosted_runner_builds_the_allowlist_network_and_pinned_agent_image() -> None:
    _assert_hosted_provisioning(_workflows())
    script = PROVISION_SCRIPT.read_text()
    for proof in (
        "docker network create --driver bridge kew-allowlist",
        "agent_image_inputs_digest",
        'configured == expected or sys.exit(f"claude-code image pin mismatch',
        "p.is_symlink()",
        'docker build --label "$label" -f docker/agent.Dockerfile -t "$image" .',
        'if [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then',
    ):
        assert proof in script, f"provisioning lacks {proof!r}"
    assert "switchyard.Dockerfile" not in script


def test_hosted_provisioning_assertions_fail_red_for_forbidden_mutations() -> None:
    def on_every_runner(workflows: dict[str, dict]) -> None:
        for step in workflows["ci.yml"]["jobs"]["test"]["steps"]:
            if step.get("name") == PROVISION_STEP:
                del step["if"]

    def after_the_check(workflows: dict[str, dict]) -> None:
        steps = workflows["release.yml"]["jobs"]["serial-tests"]["steps"]
        names = [step.get("name") for step in steps]
        steps.append(steps.pop(names.index(PROVISION_STEP)))

    def soft(workflows: dict[str, dict]) -> None:
        for step in workflows["release.yml"]["jobs"]["serial-tests"]["steps"]:
            if step.get("name") == PROVISION_STEP:
                step["continue-on-error"] = True

    for mutate in (on_every_runner, after_the_check, soft):
        with pytest.raises(AssertionError):
            _assert_hosted_provisioning(_mutated(mutate))


def test_provisioning_refuses_to_run_off_the_hosted_runner(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for tool in ("docker", "uv"):
        _write_stub(bin_dir, tool, f'echo "{tool} $*" >> "{calls}"\n')
    for runner in ("self-hosted", ""):
        result = subprocess.run(  # noqa: S603 - fixed argv, script under test
            ["bash", str(PROVISION_SCRIPT)],  # noqa: S607
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
                "RUNNER_ENVIRONMENT": runner,
            },
            check=False,
        )
        assert result.returncode != 0
        assert "runs only on a GitHub-hosted runner" in result.stderr
        assert not calls.exists(), "provisioning touched Docker off the hosted runner"


def _release_image_build_step() -> str:
    steps = _load(WORKFLOWS_DIR / "release.yml")["jobs"]["sandbox-smoke"]["steps"]
    return next(
        step["run"]
        for step in steps
        if step.get("name") == "Build pinned sandbox agent image"
    )


@pytest.mark.parametrize("builder", ["provision script", "release sandbox-smoke"])
def test_hosted_builds_label_the_agent_image_with_its_inputs_digest(
    builder: str, tmp_path: Path
) -> None:
    """A launch of the pin tag refuses an image without the inputs label, so
    every hosted build of it carries the one ``kew sandbox build`` writes."""
    from kew.image_inputs import inputs_label

    repo_root = WORKFLOWS_DIR.parent.parent
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    builds = tmp_path / "builds"
    # uv runs this interpreter (kew is importable from it); docker records builds.
    _write_stub(bin_dir, "uv", f'shift 4\nexec "{sys.executable}" "$@"\n')
    _write_stub(
        bin_dir,
        "docker",
        f'[ "$1" = build ] && printf "%s\\n" "$@" > "{builds}"\nexit 0\n',
    )
    argv = (
        ["bash", str(PROVISION_SCRIPT)]
        if builder == "provision script"
        else ["bash", "-eo", "pipefail", "-c", _release_image_build_step()]
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, script under test
        argv,
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "HOME": str(tmp_path),
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    build = builds.read_text().splitlines()
    assert build[build.index("--label") + 1] == inputs_label(repo_root)


def _triggers(workflow: dict) -> set[str]:
    # PyYAML reads the bare key `on:` as the boolean True.
    on = workflow.get(True, workflow.get("on"))
    if isinstance(on, str):
        return {on}
    return set(on or ())


def test_every_pull_request_checkout_keeps_the_token_out_of_git_config() -> None:
    # Audit 2026-10-06 R2: pull_request workflows run PR-authored code (tests,
    # build hooks, scripts), much of it on the self-hosted runner. A checkout
    # that persists credentials leaves GITHUB_TOKEN in .git/config, where that
    # code can read it. No such job needs an authenticated Git operation after
    # its checkout.
    pull_request_workflows = {
        name: workflow
        for name, workflow in _workflows().items()
        if _triggers(workflow) & {"pull_request", "pull_request_target"}
    }
    assert {"ci.yml", "docs.yml"} <= set(pull_request_workflows)
    # A job-level `uses:` runs a reusable workflow whose checkouts this scan
    # cannot see; none exists today, so refuse one rather than miss it.
    reusable = [
        f"{name}:{job_name}"
        for name, workflow in pull_request_workflows.items()
        for job_name, job in workflow["jobs"].items()
        if "uses" in job
    ]
    assert reusable == []
    persisting = [
        f"{name}:{job_name}"
        for name, workflow in pull_request_workflows.items()
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
        and (step.get("with") or {}).get("persist-credentials") is not False
    ]
    assert persisting == []
