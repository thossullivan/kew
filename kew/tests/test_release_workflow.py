import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from support.workflow_runners import RUNNER_BY_EVENT_VISIBILITY

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
SERIAL_JOB = "serial-tests"
BOUNDARY_STEP = "Runner boundary — self-hosted requires a private repository"
BOUNDARY_CONDITION = (
    "runner.environment != 'github-hosted' && "
    "github.event.repository.visibility != 'private'"
)
SERIAL_COMMAND = "uv run pytest -m serial"
NONSERIAL_COMMAND = 'uv run pytest -n auto -m "not serial"'
# The Switchyard image tooling is opt-in, and the private source input is
# useless without it, so the secret is mapped only into opted-in steps.
ARCHIVE_SECRET = (
    "${{ env.KEW_SWITCHYARD_IMAGE == '1' "
    "&& secrets.KEW_SWITCHYARD_SOURCE_ARCHIVE || '' }}"
)
RELEASE_CONDITION = (
    "needs.canonical.outputs.canonical == 'true' && "
    "(github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/main')"
)
HOSTED_JOBS = {
    "canonical",
    "release",
    "tag-release",
    "sandbox-smoke",
    "publish-testpypi",
    "publish-pypi",
    "publish-npm",
}
# No publish job runs without a person's release_version: each names the input
# as well as the release job's answer.
PUBLISH_CONDITIONS = {
    "publish-testpypi": (
        "inputs.release_version != '' && "
        "needs.release.outputs.released == 'true' && "
        "vars.TESTPYPI_PUBLISHER_REGISTERED == 'true'"
    ),
    "publish-pypi": (
        "inputs.release_version != '' && "
        "needs.release.outputs.released == 'true' && "
        "vars.PUBLISH_RELEASE == 'true'"
    ),
    "publish-npm": (
        "inputs.release_version != '' && "
        "needs.release.outputs.released == 'true' && "
        "vars.PUBLISH_RELEASE == 'true'"
    ),
}


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _named_step(job: dict, name: str) -> dict:
    matches = [step for step in job.get("steps", []) if step.get("name") == name]
    assert len(matches) == 1, f"expected one {name!r} step, got {matches!r}"
    return matches[0]


def _pytest_commands(data: dict) -> list[tuple[str, str]]:
    commands = []
    for job_name, job in data["jobs"].items():
        for step in job.get("steps", []):
            for line in str(step.get("run", "")).splitlines():
                command = line.strip()
                if command.startswith("uv run pytest"):
                    commands.append((job_name, command))
    return commands


def _assert_release_serial_gate(data: dict) -> None:
    jobs = data["jobs"]
    assert SERIAL_JOB in jobs, "release workflow has no dedicated serial test job"
    assert "runner-boundary" not in jobs, "the boundary is the serial job's first step"

    serial = jobs[SERIAL_JOB]
    release = jobs["release"]
    # Self-hosted only in a private repository; hosted everywhere else.
    assert serial.get("runs-on") == RUNNER_BY_EVENT_VISIBILITY
    assert serial.get("permissions") == {"contents": "read"}
    assert SERIAL_JOB in _needs(release)
    assert "if" not in serial and "continue-on-error" not in serial
    assert "defaults" not in data and "defaults" not in serial
    assert release.get("if") == RELEASE_CONDITION

    # The boundary runs first, before checkout, and runs no repository code.
    boundary = serial.get("steps", [{}])[0]
    assert boundary.get("name") == BOUNDARY_STEP
    assert "uses" not in boundary
    assert boundary.get("if") == BOUNDARY_CONDITION, (
        "private-repository boundary does not fail closed"
    )
    assert "exit 1" in str(boundary.get("run", ""))
    assert "continue-on-error" not in boundary

    checkouts = [
        (index, step)
        for index, step in enumerate(serial.get("steps", []))
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, "serial job must have one explicit checkout"
    checkout_index, checkout = checkouts[0]
    assert (checkout.get("with") or {}).get("persist-credentials") is False

    heal = _named_step(serial, "Preflight — ensure kew/ checkout layout")
    heal_index = serial["steps"].index(heal)
    assert heal_index > checkout_index
    heal_run = str(heal.get("run", ""))
    for command in (
        "git sparse-checkout disable",
        "git checkout -f HEAD",
        "kew/pyproject.toml",
        "exit 1",
    ):
        assert command in heal_run, f"checkout repair lacks {command!r}"

    prerequisites = _named_step(serial, "Verify serial test prerequisites")
    tests = _named_step(serial, "Run serial tests")
    assert serial["steps"].index(prerequisites) < serial["steps"].index(tests)
    for step in (prerequisites, tests):
        assert "if" not in step and "continue-on-error" not in step
        assert step.get("shell", "bash") == "bash"
    prerequisite_run = str(prerequisites.get("run", ""))
    for proof in (
        "docker network inspect kew-allowlist",
        'sandbox.get("network", "allowlist") == "allowlist"',
        "agent_image_inputs_digest",
        "configured == expected",
        'proxy_image="kew-switchyard:${worker_image#*:}"',
        'docker image inspect "$worker_image"',
        'docker image inspect "$proxy_image"',
    ):
        assert proof in prerequisite_run, f"prerequisite proof lacks {proof!r}"
    assert "|| true" not in prerequisite_run

    assert tests.get("working-directory") == "kew"
    assert tests.get("run") == SERIAL_COMMAND
    assert tests.get("env") == {"KEW_SWITCHYARD_SOURCE_ARCHIVE": ARCHIVE_SECRET}
    assert "KEW_SWITCHYARD_SOURCE_ARCHIVE" not in str(tests.get("run", ""))
    all_run_text = "\n".join(
        str(step.get("run", ""))
        for job in jobs.values()
        for step in job.get("steps", [])
    )
    assert "KEW_SWITCHYARD_SOURCE_ARCHIVE" not in all_run_text

    commands = _pytest_commands(data)
    assert commands.count((SERIAL_JOB, SERIAL_COMMAND)) == 1
    assert not any(
        command == SERIAL_COMMAND for name, command in commands if name != SERIAL_JOB
    )
    assert ("release", NONSERIAL_COMMAND) in commands

    for job_name in HOSTED_JOBS:
        assert jobs[job_name].get("runs-on") == "ubuntu-latest"
    for job_name, condition in PUBLISH_CONDITIONS.items():
        assert jobs[job_name].get("if") == condition

    forbidden_credentials = {"GH_TOKEN", "GITHUB_TOKEN", "NPM_TOKEN", "PYPI_TOKEN"}
    for scope in [serial, *serial.get("steps", [])]:
        assert forbidden_credentials.isdisjoint((scope.get("env") or {}).keys())


def test_release_serial_gate_is_complete_and_fail_closed() -> None:
    _assert_release_serial_gate(yaml.safe_load(WF.read_text()))


def _remove_release_dependency(data: dict) -> None:
    data["jobs"]["release"].pop("needs", None)


def _weaken_permissions(data: dict) -> None:
    data["jobs"][SERIAL_JOB]["permissions"] = {"contents": "write"}


def _literal_self_hosted(data: dict) -> None:
    data["jobs"][SERIAL_JOB]["runs-on"] = "self-hosted"


def _persist_checkout_credentials(data: dict) -> None:
    checkout = next(
        step
        for step in data["jobs"][SERIAL_JOB]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    checkout["with"]["persist-credentials"] = True


def _remove_visibility_refusal(data: dict) -> None:
    _named_step(data["jobs"][SERIAL_JOB], BOUNDARY_STEP).pop("if", None)


def _soften_visibility_refusal(data: dict) -> None:
    _named_step(data["jobs"][SERIAL_JOB], BOUNDARY_STEP)["run"] = "echo warning"


def _continue_after_visibility_refusal(data: dict) -> None:
    _named_step(data["jobs"][SERIAL_JOB], BOUNDARY_STEP)["continue-on-error"] = True


def _boundary_after_checkout(data: dict) -> None:
    steps = data["jobs"][SERIAL_JOB]["steps"]
    steps[0], steps[1] = steps[1], steps[0]


def _remove_checkout_repair(data: dict) -> None:
    step = _named_step(
        data["jobs"][SERIAL_JOB], "Preflight — ensure kew/ checkout layout"
    )
    step["run"] = step["run"].replace("git checkout -f HEAD", "git status")


def _soften_prerequisites(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["continue-on-error"] = True


def _skip_prerequisites(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["if"] = "false"


def _remove_network_proof(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["run"] = step["run"].replace(
        "docker network inspect kew-allowlist", "docker network ls"
    )


def _remove_network_binding_check(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["run"] = step["run"].replace(
        'sandbox.get("network", "allowlist") == "allowlist"', "True"
    )


def _remove_worker_pin_check(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["run"] = step["run"].replace("configured == expected", "True")


def _remove_worker_image_proof(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["run"] = step["run"].replace(
        'docker image inspect "$worker_image"', 'echo "$worker_image"'
    )


def _replace_proxy_pin(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["run"] = step["run"].replace(
        'proxy_image="kew-switchyard:${worker_image#*:}"', 'proxy_image="alpine:latest"'
    )


def _remove_proxy_image_proof(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["run"] = step["run"].replace(
        'docker image inspect "$proxy_image"', 'echo "$proxy_image"'
    )


def _skip_serial_tests(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Run serial tests")
    step["if"] = "false"


def _soften_serial_tests(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Run serial tests")
    step["continue-on-error"] = True


def _run_prerequisites_without_errexit(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Verify serial test prerequisites")
    step["shell"] = "bash {0}"


def _run_release_after_failed_dependency(data: dict) -> None:
    data["jobs"]["release"]["if"] = f"always() && ({RELEASE_CONDITION})"


def _duplicate_serial_tests(data: dict) -> None:
    step = _named_step(data["jobs"]["release"], "Run tests")
    step["run"] += f"\n{SERIAL_COMMAND}"


def _drop_archive_mapping(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Run serial tests")
    step.pop("env", None)


def _print_archive_value(data: dict) -> None:
    step = _named_step(data["jobs"][SERIAL_JOB], "Run serial tests")
    step["run"] += '\necho "$KEW_SWITCHYARD_SOURCE_ARCHIVE"'


@pytest.mark.parametrize(
    "mutate",
    [
        _remove_release_dependency,
        _weaken_permissions,
        _literal_self_hosted,
        _persist_checkout_credentials,
        _remove_visibility_refusal,
        _soften_visibility_refusal,
        _continue_after_visibility_refusal,
        _boundary_after_checkout,
        _remove_checkout_repair,
        _soften_prerequisites,
        _skip_prerequisites,
        _remove_network_proof,
        _remove_network_binding_check,
        _remove_worker_pin_check,
        _remove_worker_image_proof,
        _replace_proxy_pin,
        _remove_proxy_image_proof,
        _skip_serial_tests,
        _soften_serial_tests,
        _run_prerequisites_without_errexit,
        _run_release_after_failed_dependency,
        _duplicate_serial_tests,
        _drop_archive_mapping,
        _print_archive_value,
    ],
)
def test_release_serial_gate_mutations_fail_assertions(
    mutate: Callable[[dict], None],
) -> None:
    data = yaml.safe_load(WF.read_text())
    mutated = deepcopy(data)
    mutate(mutated)
    with pytest.raises(AssertionError):
        _assert_release_serial_gate(mutated)


def test_sandbox_smoke_provisions_allowlist_network_before_smoke():
    data = yaml.safe_load(WF.read_text())
    steps = data["jobs"]["sandbox-smoke"]["steps"]

    network_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("run") == "docker network create --driver bridge kew-allowlist"
    )
    smoke_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "kew sandbox smoke"
    )

    assert network_index < smoke_index

    workflow_text = WF.read_text()
    assert (
        "CI's bare bridge network proves the container/launch layers only"
        in workflow_text
    )
    assert (
        "egress restriction itself is host iptables and is NOT verified by this gate"
        in workflow_text
    )


def test_testpypi_rehearsal_gated_on_registered_publisher():
    """The rehearsal must not run (and fail on unverifiable OIDC claims) until
    an operator registers the pending publisher and sets the repo variable —
    and the skip must be announced, not silent."""
    data = yaml.safe_load(WF.read_text())

    condition = data["jobs"]["publish-testpypi"]["if"]
    assert "vars.TESTPYPI_PUBLISHER_REGISTERED == 'true'" in condition

    release_steps = data["jobs"]["release"]["steps"]
    note = next(
        step
        for step in release_steps
        if step.get("name") == "Note TestPyPI rehearsal status"
    )
    assert "TESTPYPI_PUBLISHER_REGISTERED != 'true'" in note["if"]
    assert "GITHUB_STEP_SUMMARY" in note["run"]

    # The note is gated on the release alone, like publish-testpypi, so every
    # run that would rehearse announces the skip.
    assert "github.event_name" not in note["if"]

    # The skip has to be actionable: what was skipped, why, and how to enable it.
    assert "TestPyPI rehearsal SKIPPED" in note["run"]
    assert "no trusted publisher registered" in note["run"]
    assert "TESTPYPI_PUBLISHER_REGISTERED='true'" in note["run"]
    assert "release runbook" in note["run"]

    # The publish-gate note must not claim the TestPyPI rehearsal proceeds —
    # both notes can run in the same job, and that one would contradict this.
    gate_note = next(
        step for step in release_steps if step.get("name") == "Note publish gate status"
    )
    assert "TestPyPI + npm dry-run rehearsals proceed" not in gate_note["run"]


SKIP_REASON = (
    "the pinned Switchyard input includes unreleased upstream patches; "
    "it builds on the private runner until they are public"
)


def _run_prerequisites(
    tmp_path: Path, opt_in: str | None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the prerequisite step's shell against a recording docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "docker-calls"
    calls.unlink(missing_ok=True)
    (bin_dir / "docker").write_text(f'#!/bin/bash\necho "$*" >> "{calls}"\n')
    (bin_dir / "uv").write_text("#!/bin/bash\necho kew-agent:docker-0123456789ab\n")
    for tool in ("docker", "uv"):
        (bin_dir / tool).chmod(0o755)
    step = _named_step(
        yaml.safe_load(WF.read_text())["jobs"][SERIAL_JOB],
        "Verify serial test prerequisites",
    )
    variables = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
    if opt_in is not None:
        variables["KEW_SWITCHYARD_IMAGE"] = opt_in
    # GitHub runs a bash `run:` as `bash -e {0}`.
    result = subprocess.run(  # noqa: S603 - fixed argv, workflow shell under test
        ["bash", "--noprofile", "--norc", "-e", "-c", step["run"]],  # noqa: S607
        capture_output=True,
        text=True,
        env=variables,
        check=False,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result, lines


def test_opted_in_prerequisites_inspect_the_switchyard_image(tmp_path: Path) -> None:
    result, calls = _run_prerequisites(tmp_path, "1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        "network inspect kew-allowlist",
        "image inspect kew-agent:docker-0123456789ab",
        "image inspect kew-switchyard:docker-0123456789ab",
    ]
    assert "Skipping" not in result.stdout


@pytest.mark.parametrize("opt_in", [None, ""])
def test_default_prerequisites_skip_only_the_switchyard_image(
    tmp_path: Path, opt_in: str | None
) -> None:
    result, calls = _run_prerequisites(tmp_path, opt_in)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        "network inspect kew-allowlist",
        "image inspect kew-agent:docker-0123456789ab",
    ]
    assert (
        "Skipping the kew-switchyard:docker-0123456789ab prerequisite: " + SKIP_REASON
    ) in result.stdout


@pytest.mark.parametrize("opt_in", ["true", "0", "yes"])
def test_prerequisites_refuse_any_other_switch_value(
    tmp_path: Path, opt_in: str
) -> None:
    result, calls = _run_prerequisites(tmp_path, opt_in)
    assert result.returncode != 0
    assert "KEW_SWITCHYARD_IMAGE must be unset, empty or 1" in result.stdout
    assert "image inspect kew-switchyard:docker-0123456789ab" not in calls


# --- A release is cut by a person, with an explicit version ----------------

REPO_ROOT = WF.parents[2]
PREFLIGHT = REPO_ROOT / ".github" / "scripts" / "release_preflight.py"
SLUG_MODULE = REPO_ROOT / "www" / "src" / "lib" / "data" / "install.js"
RESOLVE_STEP = "Resolve the release"
CREATE_STEP = "Create the tag and the GitHub release"
BUILD_STEP = "Build the web bundle and the wheel"
SBOM_STEP = "Generate the SBOM (CycloneDX)"
UPLOAD_STEPS = ("Upload dist artifact", "Upload the release notes and SBOM artifact")
TAG_JOB = "tag-release"
RELEASED = "steps.version.outputs.released == 'true'"


def _workflow() -> dict:
    return yaml.safe_load(WF.read_text())


def _workflow_text() -> str:
    return WF.read_text()


def _on(data: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return data.get("on", data.get(True)) or {}


def _assert_dispatch_input(data: dict) -> None:
    inputs = _on(data)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"release_version"}, "only release_version is an input"
    assert inputs["release_version"].get("type") == "string"
    assert inputs["release_version"].get("default") == ""


def _assert_repository_guard(jobs: dict) -> None:
    release, canonical = jobs["release"], jobs["canonical"]
    assert "canonical" in _needs(release)
    assert release.get("if") == RELEASE_CONDITION
    assert canonical.get("permissions") == {"contents": "read"}
    assert canonical["outputs"]["canonical"] == "${{ steps.slug.outputs.canonical }}"
    slug = _named_step(canonical, "Compare this repository with the site's slug")
    assert slug.get("id") == "slug"
    assert slug.get("run") == "python3 .github/scripts/release_preflight.py canonical"


def _assert_resolve_then_release(release: dict) -> None:
    names = [step.get("name") for step in release.get("steps", [])]
    resolve = _named_step(release, RESOLVE_STEP)
    assert names.index(RESOLVE_STEP) == 1, "resolve the release before anything else"
    assert resolve.get("id") == "version"
    assert resolve.get("env") == {"RELEASE_VERSION": "${{ inputs.release_version }}"}
    assert "release_preflight.py resolve" in resolve.get("run", "")
    assert "if" not in resolve and "continue-on-error" not in resolve
    assert release["outputs"] == {
        "released": "${{ steps.version.outputs.released }}",
        "version": "${{ steps.version.outputs.version }}",
        "tag": "${{ steps.version.outputs.tag }}",
    }
    # The release job builds, checks and uploads; it never tags or releases.
    for check in (
        BUILD_STEP,
        "Twine check",
        "Verify web UI is in the wheel",
        "Cold-install smoke test",
    ):
        assert names.index(check) < names.index(SBOM_STEP), check
    for upload in UPLOAD_STEPS:
        step = _named_step(release, upload)
        assert step.get("if") == RELEASED
        assert step["uses"].startswith("actions/upload-artifact@")
        # A re-run repeats the upload; the name is taken after attempt one.
        assert step["with"].get("overwrite") is True, upload
        assert step["with"].get("if-no-files-found") == "error"
        assert names.index(SBOM_STEP) < names.index(upload)
    build = _named_step(release, BUILD_STEP)
    assert "if" not in build, "a push builds too"
    assert "bash scripts/build_web.sh" in build["run"]
    assert "uv build" in build["run"]
    assert not any(
        "gh release" in str(step.get("run", "")) for step in release.get("steps", [])
    )


def _assert_tag_release(jobs: dict) -> None:
    """The tag and the release come from tag-release alone, after the release
    job's every check, from the files that job uploaded."""
    job = jobs[TAG_JOB]
    assert _needs(job) == ["release"]
    assert job.get("if") == (
        "inputs.release_version != '' && needs.release.outputs.released == 'true'"
    )
    assert "always()" not in str(job.get("if"))
    downloads = [
        (step.get("with") or {}).get("name") for step in job["steps"] if "uses" in step
    ]
    assert downloads == ["dist", "release-files"]
    create = _named_step(job, CREATE_STEP)
    assert create["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "RELEASE_VERSION": "${{ inputs.release_version }}",
        "VERSION": "${{ needs.release.outputs.version }}",
    }
    run = create["run"]
    assert (
        'gh release create "$tag" -R "$GITHUB_REPOSITORY" --target "$GITHUB_SHA"' in run
    )
    # The tag is the person's validated input; the release job's output only
    # has to agree with it, and both checks run before any gh call.
    assert 'tag="v${RELEASE_VERSION}"' in run
    checks = run.index('[[ "$VERSION" == "$RELEASE_VERSION" ]] || refuse')
    assert run.index("^[0-9]+\\.[0-9]+\\.[0-9]+$") < checks < run.index("gh ")
    assert 'marker="run: ${GITHUB_RUN_ID}"' in run
    assert "git/matching-refs/tags/" in run
    assert "*$'\\n'\"${marker}\"$'\\n'*" in run
    # A finished release keeps its files: nothing is ever replaced.
    assert "--clobber" not in run
    assert "/assets" in run
    # The allow list is the step's own command set, no larger.
    assert set(_command_words(run)) == _TAG_JOB_COMMANDS
    # A failed or unreadable answer stops the step: errexit on, and the only
    # fallback after `||` is the refusal.
    assert run.lstrip().startswith("set -euo pipefail")
    assert re.findall(r"\|\|\s*(\S+)", run) == ["refuse"] * run.count("||")
    # Every gh call names the repository: nothing is read from a checkout.
    for line in run.splitlines():
        if re.search(r"\bgh (release|api)\b", line) and "gh api" not in line:
            assert '-R "$GITHUB_REPOSITORY"' in line, line


# Every command tag-release may run: the words its shipped step uses, and
# nothing more. `refuse` is the step's own function.
_TAG_JOB_COMMANDS = frozenset({"gh", "printf", "echo", "exit", "set", "[[", "refuse"})
# Words that open or close a compound command: never a command themselves.
_SHELL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "!", "{", "}"}
)
# Words that run the next word as the command.
_WRAPPERS = frozenset(
    {"env", "command", "exec", "time", "xargs", "sudo", "nohup", "builtin"}
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")


def _closing_paren(text: str, start: int) -> int:
    """The index of the `)` that closes the `$(` whose body starts at `start`."""
    depth, quote, index = 1, "", start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
            elif quote == '"' and text.startswith("$(", index):
                index = _closing_paren(text, index + 2)
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif text.startswith("$(", index):
            index = _closing_paren(text, index + 2)
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise AssertionError(f"unclosed $( in {text[start:]!r}")


def _split_commands(text: str) -> tuple[list[str], list[str]]:
    """(the simple commands of a shell text, the bodies of its `$(...)`).

    Splits on newline, `;`, `&`, `|` outside quotes and outside `[[ ... ]]`,
    and lifts every command substitution out to be read on its own.
    """
    segments, bodies, current = [], [], []
    quote, in_test, index = "", False, 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                current.append(text[index : index + 2])
                index += 2
                continue
            if quote == '"' and text.startswith("$(", index):
                end = _closing_paren(text, index + 2)
                bodies.append(text[index + 2 : end])
                current.append("SUBST")
                index = end + 1
                continue
            if char == quote:
                quote = ""
            current.append(char)
        elif char == "\\":
            current.append(text[index : index + 2])
            index += 2
            continue
        elif char in "'\"":
            quote = char
            current.append(char)
        elif text.startswith("$(", index):
            end = _closing_paren(text, index + 2)
            bodies.append(text[index + 2 : end])
            current.append("SUBST")
            index = end + 1
            continue
        elif text.startswith("[[", index):
            in_test = True
            current.append(char)
        elif text.startswith("]]", index):
            in_test = False
            current.append(char)
        elif char in "\n;&|" and not in_test:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    segments.append("".join(current))
    return [s.strip() for s in segments if s.strip()], bodies


def _command_word(segment: str) -> str | None:
    """The word a simple command runs, or None for a keyword or assignment."""
    words = shlex.split(segment)
    while words:
        word = words[0]
        if word in _SHELL_KEYWORDS or re.fullmatch(r"\w+\(\)", word):
            words = words[1:]
        elif word in {"for", "case", "in", "esac", ";;"}:
            return None
        elif word.endswith(")") and not word.startswith("("):
            words = words[1:]  # a case pattern
        elif _ASSIGNMENT.match(word):
            if "=(" in word:
                return None  # an array assignment holds words, not commands
            words = words[1:]
        elif word in _WRAPPERS:
            words = [w for w in words[1:] if not w.startswith("-")]
        else:
            return word
    return None


def _command_words(run: str) -> list[str]:
    """Every command word of a step's shell, `$(...)` bodies included."""
    text = "\n".join(
        line
        for line in run.replace("\\\n", " ").splitlines()
        if not line.strip().startswith("#")
    )
    pending, words = [text], []
    while pending:
        segments, bodies = _split_commands(pending.pop())
        pending += bodies
        words += [w for w in map(_command_word, segments) if w is not None]
    return words


def _disallowed_commands(run: str) -> list[str]:
    return [
        word
        for word in _command_words(run)
        if word not in _TAG_JOB_COMMANDS or "/" in word
    ]


def _assert_write_token_confined(data: dict) -> None:
    """Exactly one job may write contents, and it runs no repository code: no
    checkout, no file from the workspace executed, no action but the pinned
    artifact download. Every other job reads."""
    assert data.get("permissions") == {"contents": "read"}
    jobs = data["jobs"]
    writers = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("contents") == "write"
    ]
    assert writers == [TAG_JOB], writers
    assert jobs[TAG_JOB]["permissions"] == {"contents": "write"}
    for job_name, job in jobs.items():
        if job_name == TAG_JOB:
            continue
        granted = job.get("permissions") or {}
        assert granted.get("contents", "read") == "read", job_name
        writes = {scope for scope, level in granted.items() if level == "write"}
        assert writes <= {"id-token"}, f"{job_name} writes {writes}"
    for step in jobs[TAG_JOB]["steps"]:
        uses = str(step.get("uses", ""))
        if uses:
            assert re.fullmatch(r"actions/download-artifact@[0-9a-f]{40}", uses), uses
        assert "working-directory" not in step
        run = str(step.get("run", ""))
        assert _disallowed_commands(run) == [], step.get("name")
    checkout = next(
        step
        for step in jobs["release"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"].get("persist-credentials") is False
    assert "token" not in checkout["with"]
    assert checkout["with"].get("fetch-depth") == 0


def _assert_early_request_check(jobs: dict) -> None:
    check = _named_step(jobs["canonical"], "Check the release request")
    assert check.get("if") == "steps.slug.outputs.canonical == 'true'"
    assert check.get("env") == {"RELEASE_VERSION": "${{ inputs.release_version }}"}
    assert "release_preflight.py resolve --skip-tag-check" in check["run"]
    assert "continue-on-error" not in check


def _assert_publish_gates(jobs: dict) -> None:
    for job_name, condition in PUBLISH_CONDITIONS.items():
        assert jobs[job_name].get("if") == condition
        assert {"release", TAG_JOB} <= set(_needs(jobs[job_name]))
        assert jobs[job_name]["permissions"] == {
            "id-token": "write",
            "contents": "read",
        }
    pypi = _named_step(jobs["publish-pypi"], "Publish to PyPI")
    assert pypi["with"].get("skip-existing") is True
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
    assert jobs["publish-npm"]["environment"]["name"] == "npm"


def _assert_release_rules(data: dict) -> None:
    """The rules a person-cut release rests on, read off the workflow."""
    _assert_dispatch_input(data)
    _assert_repository_guard(data["jobs"])
    _assert_resolve_then_release(data["jobs"]["release"])
    _assert_tag_release(data["jobs"])
    _assert_write_token_confined(data)
    _assert_early_request_check(data["jobs"])
    _assert_publish_gates(data["jobs"])
    # Nothing else creates a version: no commit-subject tooling, no stub.
    dumped = yaml.safe_dump(data)
    for gone in ("semantic-release", "semantic_release", "stub_claim"):
        assert gone not in dumped, gone


def test_release_rules_hold() -> None:
    _assert_release_rules(_workflow())


def _drop_input_from_pypi(data: dict) -> None:
    data["jobs"]["publish-pypi"]["if"] = (
        "needs.release.outputs.released == 'true' && vars.PUBLISH_RELEASE == 'true'"
    )


def _drop_input_from_npm(data: dict) -> None:
    data["jobs"]["publish-npm"]["if"] = "needs.release.outputs.released == 'true'"


def _drop_skip_existing(data: dict) -> None:
    _named_step(data["jobs"]["publish-pypi"], "Publish to PyPI")["with"].pop(
        "skip-existing"
    )


def _drop_repository_guard(data: dict) -> None:
    data["jobs"]["release"]["if"] = (
        "github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/main'"
    )


def _release_on_every_run(data: dict) -> None:
    data["jobs"][TAG_JOB]["if"] = "needs.release.outputs.released == 'true'"


def _create_before_checks(data: dict) -> None:
    data["jobs"][TAG_JOB]["needs"] = []


def _expand_input_in_shell(data: dict) -> None:
    step = _named_step(data["jobs"]["release"], RESOLVE_STEP)
    step.pop("env")
    step["run"] = "RELEASE_VERSION=${{ inputs.release_version }} " + step["run"]


def _bring_back_the_stub(data: dict) -> None:
    _on(data)["workflow_dispatch"]["inputs"]["stub_claim"] = {"type": "boolean"}


def _create_before_the_sbom(data: dict) -> None:
    data["jobs"][TAG_JOB]["if"] = "always() && " + data["jobs"][TAG_JOB]["if"]


def _a_step_after_the_tag(data: dict) -> None:
    data["jobs"][TAG_JOB]["steps"].insert(
        0, {"uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"}
    )


def _run_a_script_in_the_tag_job(data: dict) -> None:
    data["jobs"][TAG_JOB]["steps"].append(
        {"name": "Attach more", "run": "bash scripts/attach.sh"}
    )


def _the_release_job_writes(data: dict) -> None:
    data["jobs"]["release"]["permissions"] = {"contents": "write"}


def _upload_without_overwrite(data: dict) -> None:
    _named_step(data["jobs"]["release"], "Upload dist artifact")["with"].pop(
        "overwrite"
    )


def _publish_before_the_tag(data: dict) -> None:
    data["jobs"]["publish-pypi"]["needs"] = ["release", "sandbox-smoke"]


def _an_unpinned_download(data: dict) -> None:
    data["jobs"][TAG_JOB]["steps"][0]["uses"] = "actions/download-artifact@v8"


def _no_run_marker(data: dict) -> None:
    step = _named_step(data["jobs"][TAG_JOB], CREATE_STEP)
    step["run"] = step["run"].replace("*$'\\n'\"${marker}\"$'\\n'*", "*")


def _edit_tag_step(data: dict, old: str, new: str) -> None:
    step = _named_step(data["jobs"][TAG_JOB], CREATE_STEP)
    assert old in step["run"], old
    step["run"] = step["run"].replace(old, new, 1)


def _ignore_a_failed_tag_lookup(data: dict) -> None:
    _edit_tag_step(data, '.object.sha")"\n', '.object.sha")" || true\n')


def _ignore_a_failed_release_listing(data: dict) -> None:
    _edit_tag_step(data, '| .id")"\n', '| .id")" || true\n')


def _run_an_artifact(data: dict) -> None:
    _named_step(data["jobs"][TAG_JOB], CREATE_STEP)["run"] += (
        "chmod +x dist/x && dist/x\n"
    )


def _run_an_artifact_through_env(data: dict) -> None:
    _named_step(data["jobs"][TAG_JOB], CREATE_STEP)["run"] += (
        "env python3 dist/hook.py\n"
    )


def _tag_from_the_untrusted_output(data: dict) -> None:
    _edit_tag_step(data, 'tag="v${RELEASE_VERSION}"', 'tag="v${VERSION}"')


def _replace_attached_files(data: dict) -> None:
    _edit_tag_step(
        data,
        '"$GITHUB_REPOSITORY" "${upload[@]}"',
        '"$GITHUB_REPOSITORY" --clobber "${upload[@]}"',
    )


def _persist_the_release_token(data: dict) -> None:
    checkout = data["jobs"]["release"]["steps"][0]
    checkout["with"]["persist-credentials"] = True
    checkout["with"]["token"] = "${{ secrets.GITHUB_TOKEN }}"


def _write_at_workflow_level(data: dict) -> None:
    data["permissions"] = {"contents": "write"}


def _a_second_writer(data: dict) -> None:
    data["jobs"]["sandbox-smoke"]["permissions"] = {"contents": "write"}


def _drop_the_early_check(data: dict) -> None:
    steps = data["jobs"]["canonical"]["steps"]
    steps.remove(_named_step(data["jobs"]["canonical"], "Check the release request"))


def _resolve_after_the_tests(data: dict) -> None:
    steps = data["jobs"]["release"]["steps"]
    resolve = _named_step(data["jobs"]["release"], RESOLVE_STEP)
    steps.remove(resolve)
    steps.insert(len(steps) - 1, resolve)


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_input_from_pypi,
        _drop_input_from_npm,
        _drop_skip_existing,
        _drop_repository_guard,
        _release_on_every_run,
        _create_before_checks,
        _expand_input_in_shell,
        _bring_back_the_stub,
        _resolve_after_the_tests,
        _create_before_the_sbom,
        _a_step_after_the_tag,
        _persist_the_release_token,
        _write_at_workflow_level,
        _a_second_writer,
        _drop_the_early_check,
        _run_a_script_in_the_tag_job,
        _the_release_job_writes,
        _upload_without_overwrite,
        _publish_before_the_tag,
        _an_unpinned_download,
        _no_run_marker,
        _ignore_a_failed_tag_lookup,
        _ignore_a_failed_release_listing,
        _run_an_artifact,
        _run_an_artifact_through_env,
        _tag_from_the_untrusted_output,
        _replace_attached_files,
    ],
)
def test_release_rule_mutations_fail_assertions(
    mutate: Callable[[dict], None],
) -> None:
    mutated = deepcopy(_workflow())
    mutate(mutated)
    with pytest.raises(AssertionError):
        _assert_release_rules(mutated)


def test_semantic_release_configuration_is_gone() -> None:
    """Commit subjects no longer drive versions: the root pyproject.toml held
    only python-semantic-release's configuration and went with it."""
    assert not (REPO_ROOT / "pyproject.toml").exists()


# --- the preflight, run as the workflow runs it ----------------------------

_GIT_IDENTITY = (
    "-c",
    "user.name=release-test",
    "-c",
    "user.email=release-test@example.invalid",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git argv in a scratch repository
        ["git", *_GIT_IDENTITY, *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _changelog(version: str) -> str:
    return (
        "# CHANGELOG\n\n"
        f"## v{version} (2026-11-11)\n\n### Features\n\n- The first public release.\n\n"
        "## v0.9.0 (2026-09-14)\n\n- Older notes.\n"
    )


def _release_tree(
    tmp_path: Path,
    *,
    version: str = "1.0.0",
    init_version: str | None = None,
    package_version: str | None = None,
    changelog: str | None = None,
) -> Path:
    """A one-commit repository with the files the preflight reads, pushed to
    a bare `origin` it can list tags on."""
    repo = tmp_path / "repo"
    files = {
        "kew/pyproject.toml": f'[project]\nname = "kew-cli"\nversion = "{version}"\n',
        "kew/src/kew/__init__.py": f'__version__ = "{init_version or version}"\n',
        "create-kew/package.json": (
            f'{{"name": "create-kew", "version": "{package_version or version}"}}\n'
        ),
        "CHANGELOG.md": _changelog(version) if changelog is None else changelog,
        "www/src/lib/data/install.js": (
            "export const REPO_SLUG = 'example-owner/kew';\n"
        ),
    }
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(tmp_path, "init", "-q", "--bare", "origin.git")
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release tree")
    _git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    _git(repo, "push", "-q", "origin", "main")
    return repo


def _preflight(
    repo: Path, *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = repo.parent / "github-output"
    output.unlink(missing_ok=True)
    variables = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(repo.parent)),
        "GITHUB_OUTPUT": str(output),
        **env,
    }
    result = subprocess.run(  # noqa: S603 - the workflow's own command
        [sys.executable, str(PREFLIGHT), "--root", str(repo), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=variables,
        check=False,
        timeout=60,
    )
    lines = output.read_text().splitlines() if output.exists() else []
    return result, dict(line.split("=", 1) for line in lines)


def _resolve(
    repo: Path,
    version: str,
    ref: str = "refs/heads/main",
    *args: str,
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    return _preflight(
        repo,
        "resolve",
        "--notes",
        str(repo.parent / "notes.md"),
        *args,
        RELEASE_VERSION=version,
        GITHUB_REF=ref,
        **env,
    )


def _head(repo: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed git argv in a scratch repository
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_no_release_version_releases_nothing(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    result, outputs = _resolve(repo, "", ref="refs/heads/feature")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"released": "false", "version": "1.0.0"}
    assert not (tmp_path / "notes.md").exists()


def test_a_matching_version_on_main_releases_with_its_changelog_notes(
    tmp_path: Path,
) -> None:
    repo = _release_tree(tmp_path)
    result, outputs = _resolve(repo, "1.0.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"released": "true", "version": "1.0.0", "tag": "new"}
    assert (tmp_path / "notes.md").read_text() == (
        "### Features\n\n- The first public release.\n"
    )


def test_the_early_check_skips_the_tag_and_still_refuses_a_bad_request(
    tmp_path: Path,
) -> None:
    repo = _release_tree(tmp_path)
    _git(repo, "tag", "v1.0.0")
    result, outputs = _resolve(repo, "1.0.0", "refs/heads/main", "--skip-tag-check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"released": "true", "version": "1.0.0"}
    result, outputs = _resolve(repo, "1.0.1", "refs/heads/main", "--skip-tag-check")
    assert result.returncode == 1
    assert "kew/pyproject.toml says 1.0.0" in result.stdout


@pytest.mark.parametrize("where", ["here", "origin"])
def test_a_rerun_reuses_the_tag_its_first_attempt_made_at_this_commit(
    tmp_path: Path, where: str
) -> None:
    repo = _release_tree(tmp_path)
    _git(repo, "tag", "v1.0.0")
    if where == "origin":
        _git(repo, "push", "-q", "origin", "v1.0.0")
        _git(repo, "tag", "-d", "v1.0.0")
    result, outputs = _resolve(
        repo,
        "1.0.0",
        "refs/heads/main",
        GITHUB_SHA=_head(repo),
        GITHUB_RUN_ATTEMPT="2",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"released": "true", "version": "1.0.0", "tag": "existing"}


def test_a_first_attempt_never_reuses_a_tag_even_at_this_commit(
    tmp_path: Path,
) -> None:
    repo = _release_tree(tmp_path)
    _git(repo, "tag", "v1.0.0")
    result, outputs = _resolve(
        repo,
        "1.0.0",
        "refs/heads/main",
        GITHUB_SHA=_head(repo),
        GITHUB_RUN_ATTEMPT="1",
    )
    assert result.returncode == 1
    assert "already exists at this commit, and this is a first attempt" in (
        result.stdout
    )
    assert outputs == {}


def test_a_rerun_refuses_a_tag_at_another_commit(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    tagged = _head(repo)
    _git(repo, "tag", "v1.0.0")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "a later commit")
    result, outputs = _resolve(
        repo,
        "1.0.0",
        "refs/heads/main",
        GITHUB_SHA=_head(repo),
        GITHUB_RUN_ATTEMPT="3",
    )
    assert result.returncode == 1
    assert f"tag v1.0.0 already exists in this checkout, at {tagged[:12]}" in (
        result.stdout
    )
    assert outputs == {}


_EMPTY_SECTION = "# CHANGELOG\n\n## v1.0.0\n\n## v0.9.0\n\n- Older.\n"


@pytest.mark.parametrize(
    ("tree", "requested", "complaint"),
    [
        ({}, "1.0.1", "kew/pyproject.toml says 1.0.0"),
        ({"init_version": "0.9.0"}, "1.0.0", "kew/src/kew/__init__.py says 0.9.0"),
        ({"package_version": "0.9.0"}, "1.0.0", "create-kew/package.json says 0.9.0"),
        ({"version": "1.0.0rc1"}, "1.0.0rc1", "is not a plain X.Y.Z version"),
        ({}, "v1.0.0", "is not a plain X.Y.Z version"),
        ({"changelog": _changelog("0.9.0")}, "1.0.0", "no non-empty section"),
        ({"changelog": _EMPTY_SECTION}, "1.0.0", "no non-empty section"),
    ],
)
def test_a_version_the_tree_does_not_state_is_refused(
    tmp_path: Path, tree: dict, requested: str, complaint: str
) -> None:
    repo = _release_tree(tmp_path, **tree)
    result, outputs = _resolve(repo, requested)
    assert result.returncode == 1
    assert complaint in result.stdout
    assert outputs == {}
    assert not (tmp_path / "notes.md").exists()


def test_a_release_off_main_is_refused(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    result, outputs = _resolve(repo, "1.0.0", ref="refs/heads/release-candidate")
    assert result.returncode == 1
    assert "from refs/heads/main, not refs/heads/release-candidate" in result.stdout
    assert outputs == {}


def test_an_existing_local_tag_is_refused(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    _git(repo, "tag", "v1.0.0")
    result, outputs = _resolve(repo, "1.0.0")
    assert result.returncode == 1
    assert "tag v1.0.0 already exists in this checkout" in result.stdout
    assert outputs == {}


def test_a_tag_only_origin_holds_is_refused(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    _git(repo, "tag", "v1.0.0")
    _git(repo, "push", "-q", "origin", "v1.0.0")
    _git(repo, "tag", "-d", "v1.0.0")
    result, outputs = _resolve(repo, "1.0.0")
    assert result.returncode == 1
    assert "tag v1.0.0 already exists on origin" in result.stdout
    assert outputs == {}


def test_an_unreachable_origin_is_refused_not_trusted(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result, outputs = _resolve(repo, "1.0.0")
    assert result.returncode == 1
    assert "could not list tags on origin" in result.stdout
    assert outputs == {}


@pytest.mark.parametrize(
    ("repository", "canonical"),
    [
        ("example-owner/kew", "true"),
        ("example-owner/kew-archive", "false"),
        ("a-fork/kew", "false"),
        ("", "false"),
    ],
)
def test_only_the_repository_the_site_names_is_canonical(
    tmp_path: Path, repository: str, canonical: str
) -> None:
    repo = _release_tree(tmp_path)
    result, outputs = _preflight(repo, "canonical", GITHUB_REPOSITORY=repository)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"canonical": canonical}


def _site_slug() -> str:
    text = SLUG_MODULE.read_text()
    match = re.search(r"^export const REPO_SLUG = '([^']+)';$", text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_the_guard_reads_the_slug_the_site_and_install_gate_read() -> None:
    """The release workflow names no repository of its own: the guard reads
    REPO_SLUG from the site's install module."""
    slug = _site_slug()
    result, outputs = _preflight(REPO_ROOT, "canonical", GITHUB_REPOSITORY=slug)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"canonical": "true"}
    assert slug not in _workflow_text()


def test_a_slug_module_without_repo_slug_fails_closed(tmp_path: Path) -> None:
    repo = _release_tree(tmp_path)
    (repo / "www/src/lib/data/install.js").write_text("export const OTHER = 1;\n")
    result, outputs = _preflight(
        repo, "canonical", GITHUB_REPOSITORY="example-owner/kew"
    )
    assert result.returncode == 1
    assert "states no REPO_SLUG" in result.stdout
    assert outputs == {}


# --- tag-release, run as the workflow runs it, against a recording gh -------

_SHA = "a" * 40
_RUN_ID = "7777"
_GH_STUB = """#!/bin/bash
echo "gh $*" >> "$GH_LOG"
if [ -n "$FAKE_FAIL" ] && [[ "$*" == *"$FAKE_FAIL"* ]]; then
  echo "HTTP 502: Bad Gateway" >&2
  exit 1
fi
case "$*" in
  "api repos/"*"/git/matching-refs/tags/"*) printf '%s\\n' "$FAKE_TAG" ;;
  "api repos/"*"/git/tags/"*) printf '%s\\n' "$FAKE_PEELED" ;;
  "api --paginate repos/"*"/assets "*) printf '%s\\n' "$FAKE_ASSETS" ;;
  "api --paginate repos/"*"/releases "*) printf '%s\\n' "$FAKE_RELEASES" ;;
  "api repos/"*"/releases/"*) printf '%s\\n' "$FAKE_BODY" ;;
  "release create "*|"release upload "*) ;;
  *) echo "unexpected: gh $*" >&2; exit 9 ;;
esac
"""
_NOT_JSON = "<html><body>502 Bad Gateway</body></html>"
_WHEEL = "kew_cli-1.0.0-py3-none-any.whl"
_SBOM = "kew-cli-1.0.0.sbom.cdx.json"


def _tag_release(
    tmp_path: Path,
    *,
    attempt: str,
    tag: str = "",
    peeled: str = "",
    releases: str = "",
    body: str = "",
    assets: str = "",
    fail: str = "",
    release_version: str = "1.0.0",
    version: str = "1.0.0",
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    """Run tag-release's step with a fake `gh` answering from the arguments:
    where the tag points (`type sha`), the release ids, a release's body and
    asset names; `fail` makes any call whose arguments contain it exit 1."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(_GH_STUB)
    (bin_dir / "gh").chmod(0o755)
    work = tmp_path / "work"
    (work / "dist").mkdir(parents=True)
    (work / "dist" / _WHEEL).write_text("wheel")
    (work / "release-files").mkdir()
    (work / "release-files" / "release-notes.md").write_text("Notes.\n")
    (work / "release-files" / _SBOM).write_text("{}")
    log = tmp_path / "gh.log"
    step = _named_step(_workflow()["jobs"][TAG_JOB], CREATE_STEP)
    variables = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "GH_LOG": str(log),
        "GH_TOKEN": "unused",
        "RELEASE_VERSION": release_version,
        "VERSION": version,
        "FAKE_ASSETS": assets,
        "FAKE_FAIL": fail,
        "GITHUB_REPOSITORY": "example-owner/kew",
        "GITHUB_SHA": _SHA,
        "GITHUB_RUN_ID": _RUN_ID,
        "GITHUB_RUN_ATTEMPT": attempt,
        "FAKE_TAG": tag,
        "FAKE_PEELED": peeled,
        "FAKE_RELEASES": releases,
        "FAKE_BODY": body,
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, workflow shell under test
        ["bash", "--noprofile", "--norc", "-e", "-c", step["run"]],  # noqa: S607
        cwd=work,
        capture_output=True,
        text=True,
        env=variables,
        check=False,
        timeout=60,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    notes = (work / "release-files" / "release-notes.md").read_text()
    return result, calls, notes


def _writes(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("gh release ")]


def test_a_first_attempt_creates_the_release_with_its_run_and_attaches_files(
    tmp_path: Path,
) -> None:
    result, calls, notes = _tag_release(tmp_path, attempt="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _writes(calls) == [
        "gh release create v1.0.0 -R example-owner/kew --target "
        f"{_SHA} --title v1.0.0 --notes-file release-files/release-notes.md",
        "gh release upload v1.0.0 -R example-owner/kew "
        f"dist/{_WHEEL} release-files/{_SBOM}",
    ]
    assert notes == f"Notes.\n\nrun: {_RUN_ID}\n"


def test_a_rerun_finishes_this_runs_half_made_release(tmp_path: Path) -> None:
    result, calls, _ = _tag_release(
        tmp_path,
        attempt="2",
        tag=f"commit {_SHA}",
        releases="42",
        body=f"Notes.\r\n\r\nrun: {_RUN_ID}\r\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call.split(" ", 3)[:3] for call in _writes(calls)] == [
        ["gh", "release", "upload"]
    ]


def test_a_rerun_through_an_annotated_tag_at_this_commit_is_finished(
    tmp_path: Path,
) -> None:
    result, calls, _ = _tag_release(
        tmp_path,
        attempt="3",
        tag="tag " + "b" * 40,
        peeled=_SHA,
        releases="42",
        body=f"run: {_RUN_ID}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(
        call.startswith("gh api repos/example-owner/kew/git/tags/") for call in calls
    )


@pytest.mark.parametrize(
    ("case", "kwargs", "complaint"),
    [
        (
            "a tag made by hand, then a re-run",
            {"attempt": "2", "tag": f"commit {_SHA}"},
            "exists with no release, so this run did not make it",
        ),
        (
            "a release another run made",
            {
                "attempt": "2",
                "tag": f"commit {_SHA}",
                "releases": "42",
                "body": "run: 12",
            },
            f"was not created by this run ({_RUN_ID})",
        ),
        (
            "a release made by hand",
            {
                "attempt": "2",
                "tag": f"commit {_SHA}",
                "releases": "42",
                "body": "Notes.",
            },
            f"was not created by this run ({_RUN_ID})",
        ),
        (
            "a tag at another commit",
            {"attempt": "2", "tag": "commit " + "c" * 40},
            f"does not point at {_SHA}, the commit this run tested",
        ),
        (
            "a tag at this commit on a first attempt",
            {"attempt": "1", "tag": f"commit {_SHA}"},
            "a first attempt never reuses a tag",
        ),
        (
            "two releases on the tag",
            {"attempt": "2", "tag": f"commit {_SHA}", "releases": "42\n43"},
            "More than one release names v1.0.0",
        ),
    ],
)
def test_tag_release_refuses_what_this_run_did_not_make(
    tmp_path: Path, case: str, kwargs: dict, complaint: str
) -> None:
    result, calls, _ = _tag_release(tmp_path, **kwargs)
    assert result.returncode == 1, case
    assert complaint in result.stdout, result.stdout + result.stderr
    assert 'Follow "When a release fails" in docs/operations.md.' in result.stdout
    assert _writes(calls) == [], case


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("the tag lookup fails", {"fail": "/git/matching-refs/"}),
        ("the release listing fails", {"fail": "/releases --jq"}),
        (
            "the asset listing fails",
            {
                "tag": f"commit {_SHA}",
                "releases": "42",
                "body": f"run: {_RUN_ID}",
                "fail": "/assets",
            },
        ),
        ("the tag lookup answers something that is not JSON", {"tag": _NOT_JSON}),
        (
            "the release listing answers something that is not JSON",
            {"releases": _NOT_JSON},
        ),
    ],
)
def test_a_failed_or_unreadable_gh_answer_stops_tag_release(
    tmp_path: Path, case: str, kwargs: dict
) -> None:
    result, calls, _ = _tag_release(tmp_path, attempt="2", **kwargs)
    assert result.returncode != 0, case
    assert _writes(calls) == [], case


@pytest.mark.parametrize(
    ("release_version", "version"),
    [
        ("1.0.0", "1.0.1"),
        ("1.0.0", '1.0.0";'),
        ("1.0.0", "1.0.0\nrm"),
        ("1.0.0", "../1.0.0"),
        ('1.0.0";', '1.0.0";'),
        ("1.0.0/x", "1.0.0/x"),
        ("1.0.0\n", "1.0.0\n"),
    ],
)
def test_a_version_that_is_not_the_persons_plain_input_is_refused_before_gh(
    tmp_path: Path, release_version: str, version: str
) -> None:
    result, calls, _ = _tag_release(
        tmp_path, attempt="1", release_version=release_version, version=version
    )
    assert result.returncode == 1
    assert "release_version" in result.stdout
    assert calls == [], "no gh call may run before the version is checked"


def test_a_half_made_release_gets_only_its_missing_files(tmp_path: Path) -> None:
    result, calls, _ = _tag_release(
        tmp_path,
        attempt="2",
        tag=f"commit {_SHA}",
        releases="42",
        body=f"run: {_RUN_ID}",
        assets=_WHEEL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _writes(calls) == [
        f"gh release upload v1.0.0 -R example-owner/kew release-files/{_SBOM}"
    ]


def test_a_complete_release_is_never_uploaded_to_again(tmp_path: Path) -> None:
    result, calls, _ = _tag_release(
        tmp_path,
        attempt="2",
        tag=f"commit {_SHA}",
        releases="42",
        body=f"run: {_RUN_ID}",
        assets=f"{_SBOM}\n{_WHEEL}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _writes(calls) == []
    assert not any("--clobber" in call for call in calls)
