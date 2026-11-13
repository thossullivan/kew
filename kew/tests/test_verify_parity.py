"""Regression guard: scripts/verify.sh must mirror the PR-gating CI.

This repo has a history of "local gate passes, CI red" incidents caused by
verify commands drifting from CI: #406 (ruff ran on src/ locally, whole tree
in CI), #315 (xenon missing locally), #252/#344 (bandit missing locally).
This test makes that failure class impossible to reintroduce silently:

* It finds every PR-gating workflow (any ``.github/workflows/*.yml`` with a
  ``pull_request`` trigger) and extracts each non-infra ``run`` step.
* It parses ``scripts/verify.sh`` and extracts each command it runs.
* Both sides are normalized to a ``(tool, args)`` signature with path
  arguments resolved relative to the repo root (so ``src/`` run from ``kew/``
  and ``kew/src/`` run from the repo root compare equal — scope mismatches
  are caught, not just missing tools).
* Every CI gate must have an identical signature in verify.sh, and vice
  versa, so the script's "mirrors CI" header stays honest in both directions.

Intentional deltas (the explicit allowlist):

* Python matrix — CI runs the pytest job on every version in
  ``EXPECTED_TEST_MATRIX``; verify.sh runs the active interpreter only
  (forcing dual local interpreters is not worth it; CI covers the other
  leg). The gate *command* is identical so no signature is exempted, but
  the matrix is pinned below: adding a leg (e.g. 3.14) fails this test
  until someone consciously updates the pin and the verify.sh header.
* ``INFRA_STEP_NAME_PREFIXES`` — CI plumbing steps (checkout, uv/python
  setup, ``uv sync``, the gitleaks *installer*, Docker daemon locking) are not
  gates. The gitleaks *scan* step is a gate; verify.sh mirrors the installer
  by failing loudly with a remediation line when gitleaks is absent.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from support.tool_pins import (
    TOOL_PINS_ENV,
    UvxCall,
    pin_assignments,
    pin_name,
    pin_problems,
    pin_reads,
    pins_read,
    tool_pins,
    uvx_calls,
)
from support.workflow_runners import (
    RUNNER_BY_EVENT_VISIBILITY,
    can_use_self_hosted,
)

try:
    import yaml
except ImportError as exc:  # pragma: no cover — loud, not silent (#406 class)
    raise ImportError(
        "test_verify_parity needs PyYAML, currently satisfied transitively "
        "(bandit/libcst depend on it). If this import ever breaks, add "
        "'pyyaml' to [dependency-groups].dev in pyproject.toml."
    ) from exc

KEW_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = KEW_DIR.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
VERIFY_SH = KEW_DIR / "scripts" / "verify.sh"

# CI steps that are pipeline plumbing, not quality gates (matched by
# step-name prefix). Keep this list tight: an unrecognized new step is
# treated as a gate, which is the safe default.
INFRA_STEP_NAME_PREFIXES = (
    "Install uv",
    "Install dependencies",
    "Set up Python",
    "Install gitleaks",  # installer only — the "Gitleaks scan" step is the gate
    # Installer only, same as gitleaks: the gate is "Agent image CVE gate",
    # which calls scripts/scan-agent-image.sh on both sides. That script
    # asserts the version it actually got, so pinning here is not the check.
    "Install Trivy",
    # Runner preflights verify host prerequisites or repair shared clones.
    # They are plumbing, so verify.sh does not mirror them.
    "Preflight",
    # Cross-workflow daemon locking is runner plumbing, including final release.
    "Docker daemon lock",
    # Refuses a self-hosted runner outside a private repository. It checks
    # where the job landed, not the code, so verify.sh has nothing to mirror.
    "Runner boundary",
)

# Allowlisted delta: CI expresses its Python 3.13 test job as a matrix.
# verify.sh runs the active interpreter only. Pinned so a matrix change forces
# a conscious decision (update this pin + the verify.sh header comment).
EXPECTED_TEST_MATRIX = ["3.13"]

# Frontend gate (#640). PR #629 shipped a svelte-check type error through the
# whole chain because CI had no frontend job and verify.sh nothing to mirror.
# The bidirectional tests below enforce parity, but deleting the gate from BOTH
# sides keeps them in parity while silently dropping coverage — so pin the exact
# signatures here. These must equal the normalized form of
# ``npm ci && npm test -- --run && npm run check``.
FRONTEND_GATE_SIGNATURES = {
    ("npm", ("ci",)),
    ("npm", ("--", "--run", "test")),
    ("npm", ("check", "run")),
}

# Containment proof gate (#948). The CI `containment` job runs on hosted Ubuntu.
# Its Docker preflight ensures the @_REQUIRES_DOCKER mount-escape test executes.
# Locally, docker may be absent and that test skips. The non-docker tests run.
# Deliberate delta from the `test` job: `pytest -v` covers all tests; this
# targeted command exists so the containment boundary is proven in its own
# dedicated job with guaranteed docker access.
CONTAINMENT_GATE_SIGNATURES = {
    ("pytest", ("-v", "kew/tests/test_sandbox_containment.py")),
}

# Lesson-channel guard gate (Task 8, spec D8). The CI `memory-channel` job runs
# this per-commit check; verify.sh mirrors it. The bidirectional parity tests
# would stay green if the command were deleted from BOTH sides at once — that
# keeps parity while dropping the D8 inline-smuggling guard entirely. Pin the
# signature + its provenance (dedicated ci.yml job) so a dual deletion or a move
# to a non-gating spot fails a dedicated test (frontend/containment pin pattern).
MEMORY_CHANNEL_GATE_SIGNATURES = {
    ("python", ("kew/scripts/check_memory_channel.py",)),
}

# API-surface drift gate (A3, #1438). `export_openapi.py --check` shipped with a
# docstring saying CI verified the committed schema, and nothing ran it — so the
# contract the frontend and docs are counted off was honour-system. It is a gate
# on both sides now; pin the signature (frontend/containment pin pattern) so a
# deletion from CI *and* verify.sh together cannot keep the bidirectional parity
# tests green while putting the schema back to honour-system.
OPENAPI_GATE_SIGNATURES = {
    ("python", ("--check", "kew/scripts/export_openapi.py")),
}

# Launch-site gate (#1166). The CI `www` job runs the site's three drift gates —
# token sync against frontend/src/app.css, svelte-check, and the build, which
# carries the no-dashes copy gate. Those gates exist so brand drift cannot recur
# silently, which is worth nothing if the job that runs them can be deleted from
# both sides at once; pin the signature (frontend/containment pin pattern).
#
# One `npm run gates` rather than three commands is deliberate: _normalize_token
# resolves path-like argument tokens against the working directory, so a bare
# `npm run build` normalizes to `www/build` wherever the site has been built and
# to `build` on a fresh checkout — green in CI, red locally. `gates` collides
# with no path on either side.
WWW_GATE_SIGNATURES = {
    ("npm", ("gates", "run")),
}

# Docs gate (#1684). Both callers must use the same repository-root script.
# Pinning the signature prevents a coordinated deletion from keeping the
# bidirectional parity checks green while dropping the docs gate entirely.
DOCS_GATE_SIGNATURE = ("bash", ("scripts/check-docs.sh",))

# Coverage floor gate (#951, delta-audit N12). The `test` job runs pytest with
# these flags; verify.sh mirrors them. The bidirectional parity tests above
# would stay green if the flags were deleted from BOTH sides at once — that
# keeps parity while silently dropping the coverage floor. Pin the flag tokens
# on both sides so a dual deletion fails a dedicated test (frontend/containment
# pin pattern). The floor *value* is intentionally not pinned (an honest floor
# adjustment shouldn't trip this); only the flags' presence is.
COVERAGE_GATE_FLAGS = ("--cov=src/kew", "--cov-fail-under=")

# Semgrep timeout guard (W2.5). The default five-second rule/file timeout left
# the largest modules partly unscanned, while the command still exited zero.
# Pin both the raised limit and strict warning handling. A timeout must either
# finish within the raised limit or make the gate fail. The limit is 120 s, not
# 30 s, because the gate runs on the developer's own machine: under a load spike
# from other work, 30 s failed scans of unchanged files that finish in seconds.
# --timeout-threshold 0: never skip a file after N rule timeouts, so EVERY
# timing-out rule is named, not only the first. --max-target-bytes 0: semgrep
# silently skips files over 1 MB with exit 0 even under --strict; a module
# that grows past that must still be scanned (or fail loudly).
SAST_GATE_FLAGS = (
    "--strict",
    "--timeout 120",
    "--timeout-threshold 0",
    "--max-target-bytes 0",
)

# Hosted-runner pin for portable PR gates. Every job names `ubuntu-latest`
# literally or uses the one fixed visibility expression, which selects the
# self-hosted runner only in a private repository. Two failure modes are
# pinned out:
#
#   1. An unplanned self-hosted assignment outside the listed host jobs, or
#      any spelling that reaches self-hosted outside a private repository
#      (tests/test_workflow_runner_visibility.py evaluates every job).
#   2. `${{ vars.RUNNER_TARGET || ... }}` — worse, because the routing decision
#      lives in a repo variable that is invisible in review and whose blast
#      radius silently inverts when the repo goes public.
#
# The exact staged matrix below distinguishes each approved assignment.
HOSTED_RUNNER = "ubuntu-latest"

# SFD-222 staged migration. These are the only CI and Docs runner assignments
# authorized. The two CI execution jobs use the self-hosted runner while the
# repository is private, until their host inputs have portable replacements.
# Each opens with the runner boundary step. Docs has no self-hosted job.
STAGED_RUNNER_MATRIX = {
    "ci.yml": {
        "lint": HOSTED_RUNNER,
        "test": RUNNER_BY_EVENT_VISIBILITY,
        "frontend": HOSTED_RUNNER,
        "www": HOSTED_RUNNER,
        "complexity": HOSTED_RUNNER,
        "sast": HOSTED_RUNNER,
        "deps": HOSTED_RUNNER,
        "containment": HOSTED_RUNNER,
        "agent-image-scan": RUNNER_BY_EVENT_VISIBILITY,
        "memory-channel": HOSTED_RUNNER,
        "secrets": HOSTED_RUNNER,
    },
    "docs.yml": {"build": HOSTED_RUNNER},
}

# The only workflows allowed on the self-hosted runner, each because the host's
# own state is the subject of the job — not because it is faster or cheaper:
#   approval.yml     — decides against the live run-store (KEW_DISPATCH_DB)
#   audit-anchor.yml — anchors the canonical local ledger's hash chain
#   canary.yml       — exercises the runner CLIs installed on that machine
# None of the three is PR-triggered. Adding a fourth is a deliberate act.
HOST_STATE_WORKFLOWS = {"approval.yml", "audit-anchor.yml", "canary.yml"}

# Workflows self-hosted for a REASON THAT EXPIRES. CI retains two host-dependent
# jobs. Release temporarily uses the trusted runner for serial tests requiring
# provisioned Docker inputs. Each workflow must return to hosted runners when
# its blocker ends. Keep this set separate from permanent host-state workflows.
GUARDED_TEMPORARY_WORKFLOWS = {"ci.yml", "release.yml"}

# The guard itself. `runs-on` is a claim about a machine; this is a check on
# where the job actually landed. Pinned as three separate strings because each
# one is independently load-bearing: the step must come first, the condition
# must refuse a non-hosted runner outside a private repository, and it must
# FAIL rather than warn. It is a second line: a fork PR can edit it, and the
# boundary is that a public repository registers no self-hosted runner.
RUNNER_BOUNDARY_STEP = "Runner boundary — self-hosted requires a private repository"
RUNNER_BOUNDARY_CONDITION = (
    "runner.environment != 'github-hosted' && "
    "github.event.repository.visibility != 'private'"
)
RUNNER_BOUNDARY_FAILS = "exit 1"

# The shared-clone heal. PR #938 added it, 7d9f3a1f deleted it when every job
# moved to a hosted runner with a fresh clone — correctly, and with a note that
# it "has to come back" if a job ever returned to a persistent runner. A job
# returned and the heal did not, and the first self-hosted run failed exactly
# as that note predicted. A note is not a mechanism; this is.
SPARSE_HEAL_STEP = "Preflight — ensure kew/ checkout layout"

# What the heal must actually DO. Asserting only that a step with the right NAME
# exists is the same mistake the runner-boundary check made: a step named
# correctly and running `echo "trust me"` satisfied it — verified.
SPARSE_HEAL_COMMANDS = (
    "sparse-checkout disable",
    "checkout -f HEAD",
    "kew/pyproject.toml",
)


def _workflow_paths() -> list[Path]:
    """Every workflow file, both extensions.

    GitHub reads `.yml` AND `.yaml`. Globbing only `.yml` meant a workflow named
    `x.yaml` was invisible to every policy in this file at once — verified: a
    pull_request-triggered, self-hosted, repo-checking-out `.yaml` workflow with
    no guard and no heal passed all 14 tests. One collection, used by all of
    them, so a rule can never be added against a narrower set than the one it
    is meant to police.
    """
    return sorted(
        [*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")],
        key=lambda path: path.name,
    )


def _condition_of(step: dict) -> str:
    """A step's ``if``, with an optional ``${{ }}`` wrapper removed.

    ``if: X`` and ``if: ${{ X }}`` are the same condition to GitHub, so pinning
    one spelling would fail a rewrite that changed nothing.
    """
    raw = str(step.get("if", "")).strip()
    if raw.startswith("${{") and raw.endswith("}}"):
        raw = raw[3:-2].strip()
    return raw


def _is_self_hosted(job: dict) -> bool:
    """True when the job reaches the self-hosted runner in a private repo."""
    return can_use_self_hosted(job)


# Fail-closed docker check in ci.yml:containment. `runs-on` is a claim about a
# machine; this is a check. Pinned because deleting it turns the containment
# proof into a silent skip on any runner without a reachable daemon.
DOCKER_PREFLIGHT_COMMAND = "docker info"

# The secrets gate's gitleaks version is part of the gate. 8.24.3 does not
# understand `.gitleaks.toml`'s `[[allowlists]]` syntax and discards it with no
# warning, so it reported this repo's history as DIRTY while 8.30.0 reported it
# CLEAN — same commits, same config. The bidirectional parity tests compare
# command *signatures*, which are identical either way, so nothing here would
# have noticed the two sides running different tools. Pin that ci.yml and
# verify.sh name the same version.
GITLEAKS_VERSION_DECLARATION = 'GITLEAKS_VERSION: "8.30.0"'
GITLEAKS_VERSION_SHELL = 'GITLEAKS_VERSION="8.30.0"'

# Shared-clone repairs remain only on self-hosted checkout jobs. Hosted jobs use
# fresh clones. The checks below pin both the remaining repair and its cause.

# Runner/wrapper tokens stripped before identifying the tool being invoked.
_WRAPPERS = {"uv", "run", "uvx"}

# Shell lines in verify.sh that are control flow / plumbing, not gates.
# `cd` is directory plumbing (the frontend gate cds into frontend/); CI expresses
# the same via a step's ``working-directory:`` so it never appears as a run command.
# `unset` and `hash` are plumbing too: verify.sh drops inherited shell state
# (BASH_ENV, hashed paths, functions) for its tools before the first gate
# (tests/test_serial_phase.py proves the effect).
_SHELL_CONTROL = re.compile(
    r"^(#|set\s|unset\s|hash\s|cd\s|if\s|then\b|else\b|elif\s|fi\b|echo\b|printf\b"
    r"|exit\b|\w+=)"
)
# `FOO=bar uv run ...` is a gate with an environment prefix, not an
# assignment: the prefix words are dropped and the command is classified.
_ASSIGNMENT_PREFIX = re.compile(r"^(?:\w+=\S*\s+)+(?=\S)")
# `NAME="$(awk ... file)"` is one shell word: an assignment, not a prefix. It
# stays a control line, so a gate tool substituted into it is still refused.
_BARE_ASSIGNMENT = re.compile(r"^\w+=")


def _is_bare_assignment(line: str) -> bool:
    try:
        words = shlex.split(line)
    except ValueError:
        return False
    return len(words) == 1 and _BARE_ASSIGNMENT.match(words[0]) is not None


# The ONE export verify.sh may carry: the xdist worker cap, which CI expresses
# via the test job's ``env:`` block (tests/test_serial_phase.py pins the two
# against each other). Matched as the exact standalone line, so an export that
# smuggles a command (`export X=1; uv run ...`) still reaches signature
# extraction and fails parity as an unmatched gate.
_XDIST_CAP_EXPORT = re.compile(
    r'^export PYTEST_XDIST_AUTO_NUM_WORKERS="\$\{PYTEST_XDIST_AUTO_NUM_WORKERS:-\d+\}"$'
)

# The other export: KEW_REQUIRE_TOOLS=1 (#2480), which CI sets workflow-wide.
# Exact line, for the same reason; test_require_tools_mode_set_in_ci_and_verify
# pins the pair.
_REQUIRE_TOOLS_EXPORT = re.compile(r"^export KEW_REQUIRE_TOOLS=1$")

Signature = tuple[str, tuple[str, ...]]


# Tools whose invocation is a gate. A control or assignment line is skipped
# as plumbing only when no command position after `;`, `&&`, `||`, `|`,
# `then`, `else` or `do` starts with one of these: `if true; then uv run
# pytest tests/hidden.py; fi` is a gate wearing an `if`, not plumbing.
_GATE_TOOLS = frozenset(
    {"uv", "uvx", "npm", "npx", "gitleaks", "bash", "python", "python3"}
)
_LIST_OPERATORS = re.compile(r"\s*(?:;|&&|\|\||\|)\s*")
# Keywords that precede a command: the head of `if uv run ...; then` is a
# command too, so `if`/`elif`/`while`/`until` are stripped like `then`.
_LIST_KEYWORDS = re.compile(r"^(?:if|elif|while|until|then|else|do|!)\s+")
# A command whose name is computed (`$cmd`, `"$tool" run`, `${x}`) cannot be
# classified statically; parity refuses it rather than skipping it.
_DYNAMIC_COMMAND = re.compile(r"^[\"']?\$")
# Command substitutions and backticks can run a gate tool inside a control
# line (`if [ "$(uv run pytest ...)" ]`). The one accepted use is a version
# query, which gates nothing.
_SUBSTITUTED_TOOL = re.compile(
    r"(?:\$\(|`)\s*(" + "|".join(sorted(_GATE_TOOLS)) + r")\b[^)`]*"
)
_SUBSTITUTION_ALLOWED = ("gitleaks version",)


def _hidden_commands(line: str) -> list[str]:
    """Gate-tool commands inside a control/assignment line, if any."""
    hidden = []
    for segment in _LIST_OPERATORS.split(line):
        segment = segment.strip()
        while _LIST_KEYWORDS.match(segment):
            segment = _LIST_KEYWORDS.sub("", segment)
        tool = segment.split(maxsplit=1)[0] if segment else ""
        if tool in _GATE_TOOLS or _DYNAMIC_COMMAND.match(segment):
            hidden.append(segment)
    for match in _SUBSTITUTED_TOOL.finditer(line):
        inner = match.group(0).lstrip("$(` ")
        if not inner.startswith(_SUBSTITUTION_ALLOWED):
            hidden.append(inner)
    return hidden


def _logical_lines(text: str) -> Iterator[str]:
    """Yield command lines: continuations joined, comments/control skipped.

    A control line that carries a gate-tool command after a list operator is
    not skipped silently: it fails, so a gate cannot hide inside plumbing.
    """
    text = text.replace("\\\n", " ")
    for raw in text.splitlines():
        line = raw.strip()
        if not _is_bare_assignment(line):
            line = _ASSIGNMENT_PREFIX.sub("", line)
        if (
            not line
            or _XDIST_CAP_EXPORT.match(line)
            or _REQUIRE_TOOLS_EXPORT.match(line)
        ):
            continue
        if _SHELL_CONTROL.match(line):
            if not line.startswith("#"):
                hidden = _hidden_commands(line)
                assert not hidden, (
                    f"gate command hidden inside a control line (split it onto "
                    f"its own line so parity can see it): {line!r} -> {hidden}"
                )
            continue
        yield line


def _normalize_token(token: str, cwd: Path) -> str:
    """Resolve path-like tokens to repo-root-relative POSIX form."""
    if token.startswith("-"):
        return token
    if token not in {".", ".."} and "/" not in token and not (cwd / token).exists():
        return token
    try:
        resolved = (cwd / token).resolve()
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return token


def _signature(command: str, cwd: Path) -> Signature:
    """Normalize a shell command to (tool, sorted normalized args)."""
    tokens = shlex.split(command)
    while tokens:
        if tokens[0] in _WRAPPERS:
            tokens.pop(0)
        elif tokens[0] == "--with" and len(tokens) >= 2:
            # `uvx --with X tool` / `uv run --with X tool`: X is an install
            # extra for the wrapper, not the tool itself.
            tokens.pop(0)
            tokens.pop(0)
        else:
            break
    if not tokens:
        return ("", ())
    tool = tokens.pop(0)
    args = tuple(sorted(_normalize_token(t, cwd) for t in tokens))
    return (tool, args)


def _pr_gating_workflows() -> list[Path]:
    found = []
    paths = _workflow_paths()
    for path in sorted(paths):
        data = yaml.safe_load(path.read_text())
        # YAML 1.1 parses the `on:` key as boolean True.
        triggers = data.get(True, data.get("on", {}))
        if isinstance(triggers, str):
            triggers = [triggers]
        if any(t.startswith("pull_request") for t in triggers):
            found.append(path)
    return found


def _ci_gate_signatures() -> dict[Signature, str]:
    """Every gate command in every PR-gating workflow, with provenance."""
    signatures: dict[Signature, str] = {}
    for workflow in _pr_gating_workflows():
        data = yaml.safe_load(workflow.read_text())
        for job_name, job in data["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run")
                step_name = step.get("name", "<unnamed>")
                if run is None or step_name.startswith(INFRA_STEP_NAME_PREFIXES):
                    continue
                cwd = REPO_ROOT / step.get("working-directory", ".")
                for line in _logical_lines(run):
                    where = f"{workflow.name}:{job_name}:{step_name}"
                    signatures[_signature(line, cwd)] = where
    return signatures


def _verify_signatures() -> set[Signature]:
    return {_signature(line, KEW_DIR) for line in _logical_lines(VERIFY_SH.read_text())}


@pytest.mark.policy
def test_ci_yml_is_pr_gating() -> None:
    """Sanity: signature extraction found the PR gate at all."""
    workflows = [p.name for p in _pr_gating_workflows()]
    assert "ci.yml" in workflows


@pytest.mark.policy
def test_every_ci_gate_has_a_verify_sh_counterpart() -> None:
    ci = _ci_gate_signatures()
    verify = _verify_signatures()
    missing = {sig: where for sig, where in ci.items() if sig not in verify}
    assert not missing, (
        "CI gates with no scripts/verify.sh counterpart (add the exact "
        "command — same tool, same scope — to verify.sh):\n"
        + "\n".join(f"  {where}: {sig}" for sig, where in sorted(missing.items()))
    )


@pytest.mark.policy
def test_verify_sh_has_no_phantom_gates() -> None:
    """verify.sh must not claim gates CI does not run (keeps header honest)."""
    ci = set(_ci_gate_signatures())
    extra = _verify_signatures() - ci
    assert not extra, (
        "scripts/verify.sh runs commands with no CI counterpart (either add "
        "the gate to CI or drop it from verify.sh):\n"
        + "\n".join(f"  {sig}" for sig in sorted(extra))
    )


REQUIRE_TOOLS_VAR = "KEW_REQUIRE_TOOLS"


@pytest.mark.policy
def test_require_tools_mode_set_in_ci_and_verify() -> None:
    """No silent skips on either gate (#2480): both run pytest with the mode on.

    CI sets it once for the whole workflow, and no job, step or run line may
    take it back. verify.sh exports it before its first pytest and never
    mentions it again outside comments.
    """
    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert ci["env"][REQUIRE_TOOLS_VAR] == "1"
    for job_name, job in ci["jobs"].items():
        assert REQUIRE_TOOLS_VAR not in (job.get("env") or {}), job_name
        for step in job.get("steps", []):
            where = f"{job_name}: {step.get('name', '<unnamed>')}"
            assert REQUIRE_TOOLS_VAR not in (step.get("env") or {}), where
            assert REQUIRE_TOOLS_VAR not in (step.get("run") or ""), where

    commands = [
        line.strip()
        for line in VERIFY_SH.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    mentions = [i for i, line in enumerate(commands) if REQUIRE_TOOLS_VAR in line]
    assert [commands[i] for i in mentions] == [f"export {REQUIRE_TOOLS_VAR}=1"]
    first_pytest = next(i for i, line in enumerate(commands) if "pytest" in line)
    assert mentions[0] < first_pytest


@pytest.mark.policy
def test_frontend_gate_present_in_ci_and_verify() -> None:
    """The frontend gate (#640) exists on both sides — deletion can't slip by.

    Removing it from CI *and* verify.sh together would keep the bidirectional
    parity tests green while dropping the gate entirely; this pins its presence.
    """
    ci = set(_ci_gate_signatures())
    verify = _verify_signatures()
    for sig in sorted(FRONTEND_GATE_SIGNATURES):
        assert sig in ci, f"frontend gate {sig} missing from PR-gating CI (ci.yml)"
        assert sig in verify, f"frontend gate {sig} missing from scripts/verify.sh"


@pytest.mark.policy
def test_www_gate_present_in_ci_and_verify() -> None:
    """The launch-site gate (#1166) exists on both sides — deletion can't slip by.

    The site shipped with three drift gates and no job to run them, so they were
    honour-system for as long as that lasted. Removing the job from CI *and*
    verify.sh together would keep the bidirectional parity tests green while
    putting them back to honour-system; this pins its presence.
    """
    ci = _ci_gate_signatures()
    verify = _verify_signatures()
    for sig in sorted(WWW_GATE_SIGNATURES):
        where = ci.get(sig)
        assert where is not None, f"www gate {sig} missing from PR-gating CI (ci.yml)"
        assert where.startswith("ci.yml:www:"), (
            f"www gate {sig} must live in the dedicated ci.yml:www job "
            f"(found in {where})"
        )
        assert sig in verify, f"www gate {sig} missing from scripts/verify.sh"


@pytest.mark.policy
def test_docs_gate_present_in_docs_workflow_and_verify() -> None:
    """The strict docs gate uses one script on both execution paths."""
    ci = _ci_gate_signatures()
    verify = _verify_signatures()
    where = ci.get(DOCS_GATE_SIGNATURE)
    assert where is not None, "docs gate missing from PR-gating CI"
    assert where.startswith("docs.yml:build:"), (
        f"docs gate must live in docs.yml's build job (found in {where})"
    )
    assert DOCS_GATE_SIGNATURE in verify, "docs gate missing from scripts/verify.sh"


@pytest.mark.policy
def test_containment_gate_present_in_ci_and_verify() -> None:
    """The containment gate (#948) exists on both sides — deletion can't slip by.

    Removing it from CI *and* verify.sh together would keep the bidirectional
    parity tests green while silently dropping the containment boundary proof;
    this pins its presence. When docker is absent locally the @_REQUIRES_DOCKER
    test skips, but the gate command still runs and exercises the non-docker
    egress/guard tests.

    Provenance is pinned, not just existence: the gate must live in the
    dedicated ``ci.yml:containment`` job, and that job must keep its fail-closed
    docker preflight. Otherwise the test would still pass if the command were
    moved to a job on a runner without docker (where the escape test silently
    skips) or the dedicated containment job were deleted — exactly the
    green-but-unproven gate this guards against.

    The docker *preflight* is what's pinned, not a runner label: a label is a
    claim about a machine, while `docker info` is a check. That distinction is
    why the job could move to a hosted runner at all.
    """
    ci = _ci_gate_signatures()  # keep provenance (Signature -> "wf:job:step")
    verify = _verify_signatures()
    for sig in sorted(CONTAINMENT_GATE_SIGNATURES):
        where = ci.get(sig)
        assert where is not None, (
            f"containment gate {sig} missing from PR-gating CI (ci.yml)"
        )
        assert where.startswith("ci.yml:containment:"), (
            f"containment gate {sig} must live in the dedicated ci.yml:containment "
            f"job (found in {where}); a hosted-runner job would let the "
            f"@_REQUIRES_DOCKER escape test silently skip."
        )
        assert sig in verify, f"containment gate {sig} missing from scripts/verify.sh"

    ci_data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    steps = ci_data["jobs"]["containment"].get("steps", [])
    assert DOCKER_PREFLIGHT_COMMAND in _steps_run_text(steps), (
        "ci.yml:containment lost its fail-closed docker preflight "
        f"({DOCKER_PREFLIGHT_COMMAND!r}). Without it, a runner that lacks a "
        "reachable docker daemon skips the @_REQUIRES_DOCKER mount-escape test "
        "and the gate goes green without proving containment."
    )


@pytest.mark.policy
def test_memory_channel_gate_present_in_ci_and_verify() -> None:
    """The D8 lesson-channel guard (Task 8) exists on both sides.

    Deleting the guard from CI *and* verify.sh together would keep the
    bidirectional parity tests green while silently reopening the inline-
    smuggling channel D8 closes; this pins its presence. Provenance is pinned
    too: the gate must live in the dedicated ``ci.yml:memory-channel`` job, so
    moving it to a non-PR-gating spot also fails here.
    """
    ci = _ci_gate_signatures()  # keep provenance (Signature -> "wf:job:step")
    verify = _verify_signatures()
    for sig in sorted(MEMORY_CHANNEL_GATE_SIGNATURES):
        where = ci.get(sig)
        assert where is not None, (
            f"memory-channel gate {sig} missing from PR-gating CI (ci.yml)"
        )
        assert where.startswith("ci.yml:memory-channel:"), (
            f"memory-channel gate {sig} must live in the dedicated "
            f"ci.yml:memory-channel job (found in {where})."
        )
        assert sig in verify, (
            f"memory-channel gate {sig} missing from scripts/verify.sh"
        )


@pytest.mark.policy
def test_openapi_drift_gate_present_in_ci_and_verify() -> None:
    """The API-surface drift gate (A3) exists on both sides.

    ``openapi.json`` is committed and read as the API contract; before this
    gate ran, nothing checked it against the app. Deleting the check from CI
    *and* verify.sh together would keep the bidirectional parity tests green
    while returning the contract to honour-system, so pin its presence and its
    provenance: it must live in ci.yml's ``lint`` job, which is PR-gating.
    """
    ci = _ci_gate_signatures()  # keep provenance (Signature -> "wf:job:step")
    verify = _verify_signatures()
    for sig in sorted(OPENAPI_GATE_SIGNATURES):
        where = ci.get(sig)
        assert where is not None, (
            f"openapi drift gate {sig} missing from PR-gating CI (ci.yml)"
        )
        assert where.startswith("ci.yml:lint:"), (
            f"openapi drift gate {sig} must live in ci.yml's lint job "
            f"(found in {where})."
        )
        assert sig in verify, f"openapi drift gate {sig} missing from scripts/verify.sh"


@pytest.mark.parametrize(
    ("line", "hidden"),
    [
        (
            "if true; then uv run pytest tests/hidden.py; fi",
            ["uv run pytest tests/hidden.py"],
        ),
        (
            "if uv run pytest tests/hidden.py; then :; fi",
            ["uv run pytest tests/hidden.py"],
        ),
        (
            "while uv run pytest tests/hidden.py; do break; done",
            ["uv run pytest tests/hidden.py"],
        ),
        (
            'if [ -n "$(uv run pytest tests/hidden.py)" ]; then :; fi',
            ["uv run pytest tests/hidden.py"],
        ),
        ("if [ -n `npm run check` ]; then :; fi", ["npm run check"]),
        (
            'if true; then "$runner" run pytest tests/hidden.py; fi',
            ['"$runner" run pytest tests/hidden.py'],
        ),
        ("echo done | uv run python -", ["uv run python -"]),
        # Plumbing that is really plumbing.
        ('if [ "$(gitleaks version)" != "8.30.0" ]; then', []),
        ("if ! command -v gitleaks >/dev/null 2>&1; then", []),
        ('if grep -q "ERROR:xenon:" "$xenon_log"; then', []),
        ('echo "verify.sh: run uv run pytest yourself" >&2', []),
        ('xenon_output=$(<"$xenon_log")', []),
    ],
)
def test_hidden_commands_table(line: str, hidden: list[str]) -> None:
    assert _hidden_commands(line) == hidden


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("FOO=bar uv run pytest tests/x.py", ["uv run pytest tests/x.py"]),
        ("A=1 B=2 npm run check", ["npm run check"]),
        ('xenon_log="${TMPDIR:-/tmp}/kew-xenon-$$.log"', []),
        ("unset BASH_ENV\nhash -r\nunset -f uv uvx", []),
        (
            "export PYTEST_XDIST_AUTO_NUM_WORKERS="
            '"${PYTEST_XDIST_AUTO_NUM_WORKERS:-6}"',
            [],
        ),
        ("export KEEP=1", ["export KEEP=1"]),
        ("uv run ruff check . \\\n  --fix", ["uv run ruff check .    --fix"]),
    ],
)
def test_logical_lines_table(text: str, lines: list[str]) -> None:
    assert list(_logical_lines(text)) == lines


def test_logical_lines_refuse_a_gate_hidden_in_control() -> None:
    with pytest.raises(AssertionError, match="hidden inside a control line"):
        list(_logical_lines("if true; then uv run pytest tests/hidden.py; fi"))


EXPECTED_PYTHON = EXPECTED_TEST_MATRIX[0]
MATRIX_PYTHON = "${{ matrix.python-version }}"
# The ONE job allowed to pin through the matrix expression: its matrix is
# pinned exactly by test_python_test_matrix_pin. Every other job pins the
# literal, so a new matrix job cannot smuggle in another interpreter.
MATRIX_PINNED_JOB = ("ci.yml", "test")
_UV_PYTHON_INSTALL = re.compile(r"^\s*uv python install (.+?)\s*$")
_BLANKS = re.compile(r"[ \t]+")
# A command position: line start, after a list operator, subshell or group
# opener, backtick, command substitution, or a keyword that precedes a command.
_CMD = r"(?:^|[;&|({`]\s*|\$\(\s*|\b(?:if|elif|while|until|then|do|else|exec)\s+)"
# A statement position for a bare word: as above, minus `(`, `{` and a bare
# backtick, so `(kew image)` and "`kew`" inside prose are not launches.
_STATEMENT = r"(?:^|[;&|]\s*|\$\(\s*|\b(?:if|elif|while|until|then|do|else|exec)\s+)"
# `env -u X cmd` and `VAR=value cmd` still run cmd.
_PREFIX = r"(?:env(?:\s+-\w+(?:\s+\w+)?)*\s+|(?:\w+=\S*\s+)+)?"
# kew's Python through uv, where UV_PYTHON applies.
_UV_LAUNCH = re.compile(
    _CMD + _PREFIX + r"uv (?:sync|run|tool run|tool install)\b|\buvx\b[^\n]*\bkew\b",
    re.M,
)
# kew's Python NOT through uv: the interpreter is whatever `python`/`kew`
# resolves to, which no job-level pin controls. Refused outright. A bare
# `kew` counts at a statement position and may end the input.
_DIRECT_LAUNCH = re.compile(
    _CMD + _PREFIX + r"(?:python[23]?(?:\.\d+)? -m kew\b|pip[23]? install\b)"
    r"|" + _STATEMENT + _PREFIX + r"kew(?:\s|$)",
    re.M,
)
_LAUNCH_WORD = re.compile(r"[a-z]")
# The single accepted bare `kew`: uv's own shim, right after this step built
# it from the kew wheel with `uv tool install` (the release cold-install
# smoke). uv builds that environment under UV_PYTHON.
_UV_TOOL_INSTALL_WHEEL = re.compile(_CMD + r"uv tool install\b[^\n]*\.whl\b", re.M)
# A run body built from a matrix value cannot be classified: fail closed.
_MATRIX_COMMAND = re.compile(r"\$\{\{\s*matrix\.")
# Entry points this inventory cannot read: fail closed.
_MAKE = re.compile(_CMD + r"make\b", re.M)
_NPM_RUN = re.compile(r"\bnpm run\b")
# Ways a step or a followed script could move uv off the job-level pin: any
# mention of a selector (an `UV_PYTHON<<EOF >> $GITHUB_ENV` write, an
# `unset`, an `env -u` included), a uv-level interpreter flag, a project
# pin file, or a redirected environment.
_SELECTOR_MUTATION = re.compile(
    r"\bUV_PYTHON\w*\b|\bUV_PROJECT_ENVIRONMENT\b|\bVIRTUAL_ENV\b"
    r"|\buv python pin\b|\.python-version\b"
    r"|\buv\b(?: \S+)*? (?:--python|-p)\b"
)
_OVERRIDE_ENV_KEYS = ("UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")
_SETUP_PYTHON_ACTION = re.compile(r"setup-python")
# Repo scripts a run body hands off to, followed recursively: through an
# interpreter (with flags), `source`, or executed directly by path.
_REPO_SCRIPT = re.compile(
    _CMD + r"(?:(?:bash|sh|source)\s+(?:-\w+\s+)*)?((?:\.\./|\./|/)*[\w./-]+\.sh)\b",
    re.M,
)
_MAX_SCRIPT_DEPTH = 5


def _normalized(text: str) -> str:
    """Canonical token spelling: continuations joined, blanks collapsed,
    indentation stripped."""
    text = text.replace("\\\n", " ")
    return "\n".join(_BLANKS.sub(" ", line).strip() for line in text.splitlines())


def _install_versions(steps: list[dict]) -> list[str]:
    found = []
    for step in steps:
        match = _UV_PYTHON_INSTALL.match(str(step.get("run") or ""))
        if match:
            found.append(match.group(1))
    return found


def _inside_repo(path: Path) -> bool:
    resolved = path.resolve()
    root = REPO_ROOT.resolve()
    return resolved == root or root in resolved.parents


def _resolve_script(rel: str, from_dir: Path) -> Path:
    candidates = [Path(rel)] if rel.startswith("/") else []
    for base in (from_dir, REPO_ROOT, KEW_DIR):
        candidates.append(base / rel)
    stripped = re.sub(r"^(\.\./|\./)+", "", rel)
    for base in (from_dir, REPO_ROOT, KEW_DIR):
        candidates.append(base / stripped)
    for candidate in candidates:
        if candidate.is_file():
            assert _inside_repo(candidate), (
                f"a workflow runs {rel!r}, which resolves outside the repository; "
                "the interpreter inventory cannot classify it"
            )
            return candidate.resolve()
    raise AssertionError(
        f"a workflow runs {rel!r}, which resolves to no repo script; the "
        "interpreter inventory cannot classify it"
    )


def _followed_scripts(
    text: str, from_dir: Path, seen: set[Path], depth: int = 0
) -> str:
    """The text of every repo script ``text`` hands off to, recursively."""
    assert depth <= _MAX_SCRIPT_DEPTH, "script hand-off chain too deep to classify"
    collected = ""
    for rel in _REPO_SCRIPT.findall(text):
        script = _resolve_script(rel, from_dir)
        if script in seen:
            continue
        seen.add(script)
        body = _normalized(script.read_text())
        collected += (
            "\n" + body + _followed_scripts(body, script.parent, seen, depth + 1)
        )
    return collected


def _job_texts(job: dict) -> tuple[list[str], str]:
    """(one normalised run body per step, the followed scripts' text)."""
    steps = [_normalized(str(s.get("run") or "")) for s in job.get("steps") or []]
    scripts = _followed_scripts("\n".join(steps), REPO_ROOT, set())
    return steps, scripts


@functools.cache
def _npm_scripts_run_kew() -> bool:
    """Does any package.json script in the repo launch kew's Python?"""
    for manifest in REPO_ROOT.rglob("package.json"):
        if "node_modules" in manifest.parts:
            continue
        scripts = json.loads(manifest.read_text()).get("scripts") or {}
        text = _normalized("\n".join(str(v) for v in scripts.values()))
        if _UV_LAUNCH.search(text) or _DIRECT_LAUNCH.search(text):
            return True
    return False


def _runs_kew(text: str) -> bool:
    if _UV_LAUNCH.search(text) or _DIRECT_LAUNCH.search(text):
        return True
    if _MATRIX_COMMAND.search(text):
        return True
    return bool(_NPM_RUN.search(text)) and _npm_scripts_run_kew()


def _job_inventory() -> list[tuple[str, str, dict, list[str], str]]:
    """(workflow, job, spec, step bodies, scripts text) for every kew job.

    Fails closed on execution surfaces this inventory cannot read: a
    reusable-workflow job (``jobs.<id>.uses``), a local composite action
    (``uses: ./...``), or a Makefile target would run code the classifier
    never sees.
    """
    found = []
    for path in _workflow_paths():
        data = yaml.safe_load(path.read_text())
        for name, job in (data.get("jobs") or {}).items():
            where = f"{path.name}:{name}"
            assert "uses" not in job, (
                f"{where} is a reusable-workflow job; extend the interpreter "
                "inventory before adding one"
            )
            for step in job.get("steps") or []:
                uses = str(step.get("uses") or "")
                assert not uses.startswith("./"), (
                    f"{where} runs local action {uses!r}; extend the interpreter "
                    "inventory before adding one"
                )
            steps, scripts = _job_texts(job)
            text = "\n".join(steps) + scripts
            assert not _MAKE.search(text), (
                f"{where} runs make; extend the interpreter inventory before adding one"
            )
            if _runs_kew(text):
                found.append((path.name, name, job, steps, scripts))
    return found


def _launch_form(hit: re.Match[str]) -> str:
    """The launch itself, without the position prefix (`$(`, `;`, `env -u X`)."""
    form = hit.group(0).strip()
    for token in ("python", "pip", "kew"):
        idx = form.find(token)
        if idx >= 0:
            return form[idx:]
    return form


def _direct_launch_problems(where: str, steps: list[str], scripts: str) -> list[str]:
    problems = []
    for body in steps:
        shim_ok = _UV_TOOL_INSTALL_WHEEL.search(body) is not None
        for hit in _DIRECT_LAUNCH.finditer(body):
            form = _launch_form(hit)
            if form.startswith("kew") and shim_ok:
                continue  # uv's shim, built from the wheel in this same step
            problems.append(f"{where}: launches kew outside uv ({form!r})")
    for hit in _DIRECT_LAUNCH.finditer(scripts):
        form = _launch_form(hit)
        problems.append(f"{where}: a followed script launches kew outside uv: {form!r}")
    return problems


def _selector_problems(
    where: str, steps: list[dict], bodies: list[str], scripts: str
) -> list[str]:
    problems = []
    for step, body in zip(steps, bodies, strict=True):
        label = step.get("name") or step.get("run") or step.get("uses")
        if any(k in (step.get("env") or {}) for k in _OVERRIDE_ENV_KEYS):
            problems.append(f"{where}: step {label!r} overrides via env")
        if _SETUP_PYTHON_ACTION.search(str(step.get("uses") or "")):
            problems.append(f"{where}: step {label!r} installs its own Python")
        if _UV_PYTHON_INSTALL.match(body):
            continue  # the pinned install step, checked by the caller
        hit = _SELECTOR_MUTATION.search(body)
        if hit:
            problems.append(
                f"{where}: step {label!r} touches the interpreter selection "
                f"({hit.group(0)!r})"
            )
    hit = _SELECTOR_MUTATION.search(scripts)
    if hit:
        problems.append(
            f"{where}: a followed script touches the interpreter selection "
            f"({hit.group(0)!r})"
        )
    return problems


def test_python_test_matrix_pin() -> None:
    """The 3.13 leg is the one allowlisted CI-vs-verify.sh delta — pin it.

    The whole ``matrix`` mapping is compared, not just ``python-version``: a
    ``matrix.include`` entry adds a real leg with the list untouched (Codex
    review of #1820). The interpreter wiring is pinned with it, so the leg
    that exists is the leg that runs.
    """
    data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    job = data["jobs"]["test"]
    assert len(EXPECTED_TEST_MATRIX) == 1, "kew tests one interpreter (#1816)"
    assert job["strategy"]["matrix"] == {"python-version": EXPECTED_TEST_MATRIX}, (
        "CI test matrix changed (a dimension, include:, or exclude:). verify.sh "
        "intentionally runs only the active interpreter; update "
        "EXPECTED_TEST_MATRIX and the delta note in scripts/verify.sh's header."
    )
    assert job["env"]["UV_PYTHON"] == MATRIX_PYTHON, (
        "the test job does not run uv on the matrix interpreter"
    )
    assert _install_versions(job["steps"]) == [MATRIX_PYTHON], (
        "the test job does not install exactly the matrix interpreter"
    )


@pytest.mark.policy
def test_every_job_running_kew_python_pins_the_tested_interpreter() -> None:
    """Live workflows must run the interpreter CI tests, not "whatever fits".

    ``requires-python = ">=3.13"`` accepts 3.14; a job with no pin runs kew on
    whichever compatible interpreter the runner has (Codex review of #1820:
    approval.yml, audit-anchor.yml, canary.yml). Every job that executes kew
    (see ``_runs_kew``, including through repo scripts and npm scripts) runs
    it through uv, pins ``UV_PYTHON`` to the tested version at job level,
    installs exactly that version, and nothing moves off the pin: no
    selector in a step env, run body or followed script, no ``--python``
    flag, no pin file, no ``setup-python``. Only the matrix-pinned test job
    may use the matrix expression.
    """
    running = _job_inventory()
    assert len(running) >= 8, f"inventory found only {len(running)} kew-running jobs"
    problems = []
    for workflow, name, job, bodies, scripts in running:
        where = f"{workflow}:{name}"
        allowed = [EXPECTED_PYTHON]
        if (workflow, name) == MATRIX_PINNED_JOB:
            allowed.append(MATRIX_PYTHON)
        pin = (job.get("env") or {}).get("UV_PYTHON")
        if pin not in allowed:
            problems.append(f"{where}: UV_PYTHON={pin!r}, allowed {allowed}")
        steps = job.get("steps") or []
        installs = _install_versions(steps)
        if installs != [pin]:
            problems.append(f"{where}: installs {installs}, pins {pin!r}")
        problems += _direct_launch_problems(where, bodies, scripts)
        problems += _selector_problems(where, steps, bodies, scripts)
    assert not problems, (
        f"jobs running kew off the tested interpreter (expected {EXPECTED_PYTHON}):\n"
        + "\n".join(f"  {p}" for p in problems)
    )


# Committed adversarial fixtures for the classifier (each row is a defeat a
# review found, or a legitimate form that must stay clean).
@pytest.mark.parametrize(
    ("text", "runs", "direct", "mutation"),
    [
        ("uv run kew --help", True, False, False),
        ("uv  run kew --help", True, False, False),
        ("uv\tsync", True, False, False),
        ("uv run \\\n  --python 3.14 kew", True, False, True),
        ("uvx --from kew-cli kew --help", True, False, False),
        ("uv tool run kew --help", True, False, False),
        ("python -m kew --help", True, True, False),
        ("python3.14 -m kew --help", True, True, False),
        ("pip install -e kew", True, True, False),
        ("cd kew\nkew --help", True, True, False),
        ("if kew --version; then :; fi", True, True, False),
        ("version=$(kew --version)", True, True, False),
        ("uv sync && kew", True, True, False),
        ("run: ${{ matrix.cmd }}", True, False, False),
        (
            'printf "UV_PYTHON<<EOF\\n3.14\\nEOF\\n" >> "$GITHUB_ENV"',
            False,
            False,
            True,
        ),
        ("unset UV_PYTHON", False, False, True),
        ("env -u UV_PYTHON uv run kew", True, False, True),
        ("uv python pin 3.14", False, False, True),
        ("echo 3.14 > .python-version", False, False, True),
        ("UV_PROJECT_ENVIRONMENT=/tmp/x uv run kew", True, False, True),
        # Prose and plumbing that must stay clean.
        ('echo "Checkout layout OK: kew/ present"', False, False, False),
        ('echo "scan (kew image)"', False, False, False),
        ("echo '`kew` is the CLI'", False, False, False),
        ("if [ ! -d kew ]; then exit 1; fi", False, False, False),
        ("uv python install 3.13", False, False, False),
        ("npm run check", False, False, False),
    ],
)
def test_inventory_classifier_table(
    text: str, runs: bool, direct: bool, mutation: bool
) -> None:
    body = _normalized(text)
    assert (
        _UV_LAUNCH.search(body) is not None
        or _DIRECT_LAUNCH.search(body) is not None
        or _MATRIX_COMMAND.search(body) is not None
    ) == runs
    assert (_DIRECT_LAUNCH.search(body) is not None) == direct
    assert (_SELECTOR_MUTATION.search(body) is not None) == mutation


@pytest.mark.parametrize(
    ("text", "scripts"),
    [
        ("bash scripts/x.sh", ["scripts/x.sh"]),
        ("bash -e scripts/x.sh", ["scripts/x.sh"]),
        ("sh -eu ../scripts/x.sh", ["../scripts/x.sh"]),
        ("scripts/x.sh --flag", ["scripts/x.sh"]),
        ("./scripts/x.sh", ["./scripts/x.sh"]),
        ("/abs/scripts/x.sh", ["/abs/scripts/x.sh"]),
        ("source scripts/env.sh", ["scripts/env.sh"]),
        ("if true; then bash scripts/x.sh; fi", ["scripts/x.sh"]),
        ('echo "see scripts/x.sh for details"', []),
    ],
)
def test_repo_script_detection_table(text: str, scripts: list[str]) -> None:
    assert _REPO_SCRIPT.findall(_normalized(text)) == scripts


def test_script_resolution_stays_inside_the_repository(tmp_path: Path) -> None:
    outside = tmp_path / "evil.sh"
    outside.write_text("uv run kew\n")
    with pytest.raises(AssertionError, match="outside the repository"):
        _resolve_script(str(outside), REPO_ROOT)
    with pytest.raises(AssertionError, match="no repo script"):
        _resolve_script("scripts/does-not-exist.sh", REPO_ROOT)


def _steps_run_text(steps: list[dict]) -> str:
    """Concatenate every ``run:`` body across a job's steps."""
    return "\n".join(str(s.get("run") or "") for s in steps)


def test_coverage_floor_gate_present_in_ci_and_verify() -> None:
    """The coverage floor (#951, N12) lives in BOTH ci.yml and verify.sh.

    Deleting ``--cov-fail-under`` from CI *and* verify.sh together keeps the
    bidirectional parity tests green while dropping the floor entirely; this
    pins the flags' presence on both sides (frontend/containment pin pattern).
    """
    ci_data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    ci_test_runs = _steps_run_text(ci_data["jobs"]["test"].get("steps", []))
    verify_text = VERIFY_SH.read_text()

    for flag in COVERAGE_GATE_FLAGS:
        assert flag in ci_test_runs, (
            f"coverage flag {flag!r} missing from ci.yml's `test` job — the "
            "#951 coverage floor was dropped from the PR gate."
        )
        assert flag in verify_text, (
            f"coverage flag {flag!r} missing from scripts/verify.sh — it no "
            "longer mirrors CI's coverage floor."
        )


def test_sast_timeout_gate_present_in_ci_and_verify() -> None:
    """Semgrep gets enough time, and any remaining timeout fails the gate."""
    ci_data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    ci_sast_runs = _steps_run_text(ci_data["jobs"]["sast"].get("steps", []))
    verify_text = VERIFY_SH.read_text()

    for flag in SAST_GATE_FLAGS:
        assert flag in ci_sast_runs, (
            f"Semgrep flag {flag!r} missing from ci.yml's `sast` job."
        )
        assert flag in verify_text, (
            f"Semgrep flag {flag!r} missing from scripts/verify.sh."
        )


def test_contributing_shows_the_same_semgrep_gate() -> None:
    """The command a contributor copies must be the gate CI runs."""
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text()
    for flag in SAST_GATE_FLAGS:
        assert flag in contributing, (
            f"Semgrep flag {flag!r} missing from CONTRIBUTING.md's SAST line."
        )


def _assert_ci_and_docs_runner_matrix(workflows: dict[str, dict]) -> None:
    """Require the exact per-job runner assignments for CI and Docs."""
    for workflow_name, expected in STAGED_RUNNER_MATRIX.items():
        jobs = workflows[workflow_name]["jobs"]
        found = {job_name: job.get("runs-on") for job_name, job in jobs.items()}
        assert found == expected, (
            f"{workflow_name} runner matrix changed: {found!r} vs {expected!r}"
        )


def _assert_pr_jobs_are_hosted_or_guarded(workflow_name: str, data: dict) -> None:
    """Require readable runners and a first-step guard on host-capable jobs."""
    # `on` is parsed by PyYAML 1.1 rules as the boolean True.
    triggers = data.get("on", data.get(True)) or {}
    if "pull_request" not in triggers and "pull_request_target" not in triggers:
        return

    jobs = data["jobs"]
    for job_name, job in jobs.items():
        runs_on = job.get("runs-on")
        assert runs_on in (HOSTED_RUNNER, RUNNER_BY_EVENT_VISIBILITY), (
            f"{workflow_name} job {job_name!r} has a computed or unexpected "
            f"runs-on ({runs_on!r}). A PR-triggered job names {HOSTED_RUNNER!r} "
            "or uses the fixed visibility expression, so the runner stays "
            "readable in review."
        )

    for job_name, job in jobs.items():
        if not _is_self_hosted(job):
            continue
        # Inspected as STEPS, not as a dump of the job. The condition and
        # failure must share one step, or the condition gates nothing.
        steps = job.get("steps") or []
        first = steps[0] if steps else {}
        assert first.get("name") == RUNNER_BOUNDARY_STEP, (
            f"{workflow_name} job {job_name!r} can run on the self-hosted "
            f"runner but does not open with {RUNNER_BOUNDARY_STEP!r}, so "
            "repository code could run before the boundary is checked."
        )
        assert _condition_of(
            first
        ) == RUNNER_BOUNDARY_CONDITION and RUNNER_BOUNDARY_FAILS in str(
            first.get("run", "")
        ), (
            f"{workflow_name} job {job_name!r} has no single step that both "
            f"tests {RUNNER_BOUNDARY_CONDITION!r} and runs "
            f"{RUNNER_BOUNDARY_FAILS!r}. Split across two steps the condition "
            "gates nothing and the job is decoration."
        )
        assert "uses" not in first and "continue-on-error" not in first, (
            f"{workflow_name} job {job_name!r}'s boundary runs an action or "
            "continues on error. The guard must run no repository code and "
            "must stop the job. Checked as a step KEY, not a substring: "
            "'uses' appears in ordinary prose."
        )


def _synthetic_staged_workflows() -> dict[str, dict]:
    return {
        workflow_name: {
            "jobs": {
                job_name: {"runs-on": runner} for job_name, runner in expected.items()
            }
        }
        for workflow_name, expected in STAGED_RUNNER_MATRIX.items()
    }


def _synthetic_guarded_workflow() -> dict:
    return {
        True: {"pull_request": {}},
        "jobs": {
            "portable": {"runs-on": HOSTED_RUNNER},
            "host-job": {
                "runs-on": RUNNER_BY_EVENT_VISIBILITY,
                "steps": [
                    {
                        "name": RUNNER_BOUNDARY_STEP,
                        "if": RUNNER_BOUNDARY_CONDITION,
                        "run": RUNNER_BOUNDARY_FAILS,
                    },
                    {"uses": "actions/checkout@" + "0" * 40},
                ],
            },
        },
    }


def test_ci_and_docs_runner_matrix_is_exact() -> None:
    """Check the exact runner matrix for the CI and Docs workflows."""
    workflows = {
        name: yaml.safe_load((WORKFLOWS_DIR / name).read_text())
        for name in STAGED_RUNNER_MATRIX
    }
    _assert_ci_and_docs_runner_matrix(workflows)


def test_runner_policy_assertions_fail_red_for_forbidden_mutations() -> None:
    workflows = _synthetic_staged_workflows()
    workflows["ci.yml"]["jobs"]["lint"]["runs-on"] = "self-hosted"
    with pytest.raises(AssertionError, match="runner matrix changed"):
        _assert_ci_and_docs_runner_matrix(workflows)

    _assert_pr_jobs_are_hosted_or_guarded(
        "synthetic.yml", _synthetic_guarded_workflow()
    )

    workflow = _synthetic_guarded_workflow()
    workflow["jobs"]["portable"]["runs-on"] = "${{ vars.RUNNER_TARGET }}"
    with pytest.raises(AssertionError, match="computed or unexpected runs-on"):
        _assert_pr_jobs_are_hosted_or_guarded("synthetic.yml", workflow)

    workflow = _synthetic_guarded_workflow()
    workflow["jobs"]["portable"]["runs-on"] = "self-hosted"
    with pytest.raises(AssertionError, match="computed or unexpected runs-on"):
        _assert_pr_jobs_are_hosted_or_guarded("synthetic.yml", workflow)

    workflow = _synthetic_guarded_workflow()
    steps = workflow["jobs"]["host-job"]["steps"]
    steps[0], steps[1] = steps[1], steps[0]
    with pytest.raises(AssertionError, match="does not open with"):
        _assert_pr_jobs_are_hosted_or_guarded("synthetic.yml", workflow)

    workflow = _synthetic_guarded_workflow()
    workflow["jobs"]["host-job"]["steps"][0]["run"] = "echo warning"
    with pytest.raises(AssertionError, match="has no single step"):
        _assert_pr_jobs_are_hosted_or_guarded("synthetic.yml", workflow)

    workflow = _synthetic_guarded_workflow()
    workflow["jobs"]["host-job"]["steps"][0]["if"] = (
        "github.event.repository.visibility != 'private'"
    )
    with pytest.raises(AssertionError, match="has no single step"):
        _assert_pr_jobs_are_hosted_or_guarded("synthetic.yml", workflow)


@pytest.mark.policy
def test_every_pr_triggered_job_is_hosted_or_guarded() -> None:
    """A `pull_request`-triggered job may reach self-hosted only behind the guard.

    This is a security boundary. ci.yml runs PR-authored dependencies, test code
    and npm scripts; the self-hosted machine holds credentials lateral to
    several unrelated projects. While the repo is private no fork PR can reach
    it — but that protection is a property of repo VISIBILITY, not of the
    workflow, and it inverts the moment the repo is published.

    This used to be a flat ban, which was the right shape while hosted runners
    were available. When hosted minutes ran out the ban had to be broken to
    have any CI at all, and a ban that gets broken under pressure protects
    nothing. So a PR-triggered job may reach the self-hosted machine only
    through the fixed visibility expression, which selects it only in a
    private repository, and it must open with a step that hard-fails on a
    non-hosted runner outside a private repository. A fork PR can edit both,
    so neither is the boundary: a public repository registers no self-hosted
    runner (docs/operations.md, "Public repository runner rule").

    ``runs-on`` is a literal or that one fixed expression, never a variable:
    routing the decision through ``vars.RUNNER_TARGET`` puts it somewhere
    review cannot see it, which is how it would come back.
    """
    for path in _workflow_paths():
        data = yaml.safe_load(path.read_text())
        _assert_pr_jobs_are_hosted_or_guarded(path.name, data)


# Every script a gate runs a `uvx` tool from, beside every workflow (#3245). A
# script loads kew/scripts/tool-pins.env by a path relative to its own
# directory or that directory's parent (where each of them cds), and a
# workflow step by one relative to its working-directory.
UVX_GATE_SCRIPT_GLOBS = ("kew/scripts/*.sh", "scripts/*.sh", "www/*.sh")
# The pages whose semgrep command a contributor copies and runs by hand.
COPYABLE_SEMGREP_PAGES = ("AGENTS.md", "CONTRIBUTING.md")
# kew's check image installs the semgrep the gate runs (`uv tool install`), at
# the versions its build script reads from the pin file and passes in.
CHECK_IMAGE_RECIPE = REPO_ROOT / "scripts" / "check-image.Dockerfile"
CHECK_IMAGE_BUILD = REPO_ROOT / "scripts" / "build-check-image.sh"


def _loads_the_pin(name: str, text: str, cwds: tuple[Path, ...]) -> bool:
    reads = pin_reads(text)
    return any(
        (cwd / path).resolve() == TOOL_PINS_ENV.resolve()
        for path in reads["*"] + reads.get(name, [])
        for cwd in cwds
    )


def _the_image_is_handed_the_pin(name: str) -> bool:
    """The recipe takes ``name`` as a build argument with no default, and the
    build script loads it from the pin file and passes it on unchanged."""
    recipe = CHECK_IMAGE_RECIPE.read_text()
    build = CHECK_IMAGE_BUILD.read_text()
    return (
        re.search(rf"^ARG {name}\s*$", recipe, re.M) is not None
        and _loads_the_pin(name, build, (REPO_ROOT,))
        and f'--build-arg "{name}=${{{name}}}"' in build
    )


def _gate_texts() -> list[tuple[str, str, tuple[Path, ...]]]:
    """Every script and workflow step a gate runs: where, text, its cwds."""
    texts: list[tuple[str, str, tuple[Path, ...]]] = []
    for pattern in UVX_GATE_SCRIPT_GLOBS:
        for script in sorted(REPO_ROOT.glob(pattern)):
            cwds = (script.parent, script.parent.parent)
            texts.append(
                (script.relative_to(REPO_ROOT).as_posix(), script.read_text(), cwds)
            )
    for workflow in _workflow_paths():
        data = yaml.safe_load(workflow.read_text())
        for job_name, job in (data.get("jobs") or {}).items():
            for step in job.get("steps", []):
                run = step.get("run")
                if run is None:
                    continue
                where = f"{workflow.name}:{job_name}:{step.get('name', '<unnamed>')}"
                cwd = REPO_ROOT / step.get("working-directory", ".")
                texts.append((where, str(run), (cwd,)))
    return texts


def _gate_uvx_calls() -> tuple[list[UvxCall], list[str]]:
    """Every gate's uvx call, and each text that reads a pin it never loads."""
    calls: list[UvxCall] = []
    unloaded: list[str] = []
    for where, text, cwds in _gate_texts():
        found = uvx_calls(text, where)
        calls.extend(found)
        unloaded.extend(
            f"{where} ({name})"
            for call in found
            for name in pins_read(call)
            if not _loads_the_pin(name, text, cwds)
        )
    where = CHECK_IMAGE_RECIPE.relative_to(REPO_ROOT).as_posix()
    found = uvx_calls(CHECK_IMAGE_RECIPE.read_text(), where, installs=True)
    assert found, f"{where} no longer installs semgrep with `uv tool install`"
    calls.extend(found)
    unloaded.extend(
        f"{where} ({name})"
        for call in found
        for name in pins_read(call)
        if not _the_image_is_handed_the_pin(name)
    )
    return calls, unloaded


@pytest.mark.policy
def test_every_uvx_tool_in_a_gate_is_pinned_to_one_exact_version() -> None:
    """Each uvx tool a gate runs is the one exact version tool-pins.env names.

    An unversioned `uvx semgrep` resolves the newest release on the day it
    runs, and a range rebuilds its environment whenever a release lands in it,
    so two runs of one gate could run two different tools (#3245). That holds
    for `uv tool run` and `uv run --with` too, for a call behind an
    environment prefix or a wrapper such as `timeout`, and for the semgrep
    kew's check image installs. Each call reads the pin rather than copying
    it. Every pin must also be used, so the file cannot keep a version no gate
    runs.
    """
    pins = tool_pins()
    calls, unloaded = _gate_uvx_calls()

    problems = [
        problem for call in calls for problem in pin_problems(call, pins, literal=False)
    ]
    assert not problems, "\n".join(problems)
    assert not unloaded, (
        "these read a pin from kew/scripts/tool-pins.env without loading it: "
        f"{unloaded}"
    )
    used = {pin_name(spec.split("==")[0]) for call in calls for spec in call.specs}
    assert used == set(pins), (
        f"tool-pins.env and the gates' uvx calls disagree: pinned {sorted(pins)}, "
        f"run {sorted(used)}"
    )


@pytest.mark.policy
def test_no_gate_sets_a_pinned_version_outside_the_pin_file() -> None:
    """A pin is set in tool-pins.env and nowhere else.

    `TWINE_VERSION=6.1.0` after `source scripts/tool-pins.env`, or an
    `ARG SEMGREP_VERSION=1.0` default in the check image's recipe, would run a
    version no pin names while every call still reads `${TWINE_VERSION}`. The
    only lines that may set one are the awk read of the file and passing it on
    under its own name.
    """
    pins = tool_pins()
    texts = [(where, text) for where, text, _ in _gate_texts()]
    texts.append(
        (
            CHECK_IMAGE_RECIPE.relative_to(REPO_ROOT).as_posix(),
            CHECK_IMAGE_RECIPE.read_text(),
        )
    )
    problems = [
        problem
        for where, text in texts
        for problem in pin_assignments(text, pins, where)
    ]
    assert not problems, "\n".join(problems)


def test_the_pin_file_reads_the_same_when_bash_sources_it() -> None:
    """The file the tests read line-wise is the file bash sources: no export."""
    pins = tool_pins()
    script = (
        f'source "$1"; for name in {" ".join(pins)}; do '
        'printf "%s=%s\\n" "$name" "${!name}"; done; env'
    )
    completed = subprocess.run(
        ["bash", "-c", script, "bash", str(TOOL_PINS_ENV)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    lines = completed.stdout.splitlines()
    assert lines[: len(pins)] == [f"{name}={value}" for name, value in pins.items()]
    exported = [line for line in lines[len(pins) :] if line.split("=", 1)[0] in pins]
    assert not exported, f"tool-pins.env exports into every child: {exported}"


def test_the_copyable_semgrep_commands_name_the_pinned_version() -> None:
    """The semgrep command a contributor copies runs the semgrep the gate runs,
    and every other pinned tool those pages show (the license gate's
    pip-licenses) is named at its pin too."""
    pins = tool_pins()
    for page in COPYABLE_SEMGREP_PAGES:
        calls = uvx_calls((REPO_ROOT / page).read_text(), page)
        semgrep = [call for call in calls if call.command.startswith("semgrep")]
        assert semgrep, f"{page} no longer shows the semgrep command"
        problems = [
            problem
            for call in calls
            for problem in pin_problems(call, pins, literal=True)
        ]
        assert not problems, "\n".join(problems)
        for call in semgrep:
            assert f"semgrep=={pins['SEMGREP_VERSION']}" in call.specs, call
            assert f"setuptools=={pins['SETUPTOOLS_VERSION']}" in call.specs, call


@pytest.mark.parametrize(
    "text",
    [
        # `-w` is uv's short --with, attached or not.
        "uv run -w pip-licenses pip-licenses --fail-on=GPL",
        "uv run -wpip-licenses pip-licenses",
        # An option before --with whose value could be read as the command.
        "uv run --resolution lowest --with pip-licenses pip-licenses",
        "uv run --color never --with pip-licenses pip-licenses",
        "uv run --exclude-newer 2026-01-01 --with pip-licenses pip-licenses",
        "uv run --index local --with pip-licenses pip-licenses",
        "uv run --cache-dir cache --with pip-licenses pip-licenses",
        # An option uv may add tomorrow: refused rather than guessed at.
        "uv run --some-new-option pip-licenses",
        # uv's global options between `uv` and its subcommand.
        "uv --directory kew run --with pip-licenses pip-licenses",
        "uv -q run --with pip-licenses pip-licenses",
        "uv --quiet tool run semgrep scan",
        # A quoted command word runs the same tool.
        '"uvx" semgrep scan',
        "'uvx' semgrep scan",
    ],
)
def test_a_call_the_pin_check_cannot_read_as_pinned_is_refused(text: str) -> None:
    calls = uvx_calls(text, "probe")

    assert calls, f"{text!r} is a call the scan never saw"
    assert [
        problem
        for call in calls
        for problem in pin_problems(call, tool_pins(), literal=False)
    ], f"{text!r} passed as pinned: {calls}"


@pytest.mark.parametrize(
    "text",
    [
        'uv -q --directory kew run -w pip-licenses=="${PIP_LICENSES_VERSION}" x',
        "uv run --resolution lowest --with=pip-licenses==${PIP_LICENSES_VERSION} x",
        "uv run -wpip-licenses==${PIP_LICENSES_VERSION} pip-licenses",
        '"uvx" --with setuptools=="${SETUPTOOLS_VERSION}" '
        'semgrep=="${SEMGREP_VERSION}"',
        "uv --quiet tool run semgrep==${SEMGREP_VERSION} scan",
        "uv run --frozen -m pytest tests/",
    ],
)
def test_a_call_that_reads_its_pins_passes(text: str) -> None:
    calls = uvx_calls(text, "probe")

    assert len(calls) == 1, f"{text!r} read as {calls}"
    assert not [
        problem
        for call in calls
        for problem in pin_problems(call, tool_pins(), literal=False)
    ]


@pytest.mark.parametrize(
    "line",
    [
        'SEMGREP_VERSION="${SEMGREP_VERSION}0"',
        "SEMGREP_VERSION=$SEMGREP_VERSION$X",
        'SEMGREP_VERSION="${SEMGREP_VERSION}"x',
        "TWINE_VERSION=6.1.0",
        "export TWINE_VERSION=6.1.0",
        "ARG SEMGREP_VERSION=1.0",
        "ENV SEMGREP_VERSION 1.0",
        'semgrep=="${SEMGREP_VERSION:=1.0.0}"',
        "read -r _ TWINE_VERSION",
    ],
)
def test_a_line_that_sets_a_pin_to_anything_but_itself_is_refused(line: str) -> None:
    assert pin_assignments(line, tool_pins(), "probe"), line


@pytest.mark.parametrize(
    "line",
    [
        '--build-arg "SEMGREP_VERSION=${SEMGREP_VERSION}" \\',
        'SEMGREP_VERSION="$SEMGREP_VERSION"',
        "SEMGREP_VERSION=${SEMGREP_VERSION}; export X",
        'SEMGREP_VERSION="$(awk -F= \'$1 == "SEMGREP_VERSION" { print $2 }\' '
        'scripts/tool-pins.env)"',
    ],
)
def test_a_line_that_reads_or_passes_on_a_pin_is_allowed(line: str) -> None:
    assert not pin_assignments(line, tool_pins(), "probe"), line


def test_gitleaks_version_is_pinned_and_agrees_across_ci_and_verify() -> None:
    """CI and verify.sh must run the SAME gitleaks build, pinned on both sides.

    A secrets scanner's version is part of the gate. The `secrets` job used to
    prefer any gitleaks already on PATH, then brew, and only fall back to a
    pinned download — so the ruleset was whatever the machine had. That is how
    this repo's history scanned clean on the self-hosted Mac (8.30.0) and dirty
    on a hosted runner (8.24.3): 8.24.3 does not support `.gitleaks.toml`'s
    `[[allowlists]]` and drops the allowlist silently.

    The bidirectional parity tests compare normalized command signatures, which
    are byte-identical whichever binary runs, so they could never have caught
    this. Hence a dedicated pin.
    """
    ci_text = (WORKFLOWS_DIR / "ci.yml").read_text()
    verify_text = VERIFY_SH.read_text()

    assert GITLEAKS_VERSION_DECLARATION in ci_text, (
        f"ci.yml no longer pins gitleaks as {GITLEAKS_VERSION_DECLARATION!r}. "
        "If the pin moved, update this test and verify.sh together — and "
        "re-check that .gitleaks.toml is still honoured by the new version."
    )
    assert GITLEAKS_VERSION_SHELL in verify_text, (
        f"scripts/verify.sh no longer pins gitleaks as "
        f"{GITLEAKS_VERSION_SHELL!r}, so a local run may disagree with CI."
    )
    assert "command -v gitleaks" not in ci_text, (
        "ci.yml's secrets job reintroduced a 'use whatever gitleaks is already "
        "installed' branch. That makes the gate's ruleset a property of the "
        "runner rather than of the repo — the exact drift this pin exists for."
    )


def test_gitleaks_history_audit_runs_the_same_pinned_build() -> None:
    """The weekly history scan is the same gate as the PR gate, or it lies.

    scheduled-audits.yml kept the prefer-PATH / brew / fall-back-to-8.24.3
    install after ci.yml moved to the pin, so on a hosted runner it ran
    8.24.3, dropped `.gitleaks.toml`'s `[[allowlists]]` silently, and re-filed
    two allowlisted false positives every week as "secrets in git history"
    (#1869) while the pinned gate and a local 8.30.0 scan were clean over the
    same commits. Pin it here too, and keep the pins equal.
    """
    audit_text = (WORKFLOWS_DIR / "scheduled-audits.yml").read_text()
    ci_text = (WORKFLOWS_DIR / "ci.yml").read_text()

    assert GITLEAKS_VERSION_DECLARATION in audit_text, (
        f"scheduled-audits.yml no longer pins gitleaks as "
        f"{GITLEAKS_VERSION_DECLARATION!r}; the history scan would run a "
        "different ruleset from the `secrets` gate."
    )

    def job_text(workflow: str, job: str) -> str:
        # A top-level job block: from its key to the next key at the same
        # two-space indentation. Other jobs may legitimately use brew.
        match = re.search(
            rf"^  {re.escape(job)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
            re.S | re.M,
        )
        assert match is not None, f"job {job!r} not found"
        return match.group(1)

    audit_job = job_text(audit_text, "gitleaks-history")
    secrets_job = job_text(ci_text, "secrets")
    for prohibited in ("command -v gitleaks", "command -v brew", "brew install"):
        assert prohibited not in audit_job, (
            f"scheduled-audits.yml's gitleaks-history job reintroduced {prohibited!r}: "
            "a 'use whatever is already installed' or package-manager branch makes "
            "the scan's ruleset a property of the runner rather than of the repo."
        )
        assert prohibited not in secrets_job, (
            f"ci.yml's secrets job reintroduced {prohibited!r}."
        )
    # The download URL must be built from the declaration, never from a
    # literal version, or the pin and the binary can silently diverge.
    assert re.search(r"gitleaks_[0-9]+\.[0-9]+\.[0-9]+", audit_text) is None, (
        "scheduled-audits.yml hardcodes a gitleaks release in a download URL "
        "instead of using ${GITLEAKS_VERSION}."
    )
    assert "gitleaks_${GITLEAKS_VERSION}_${asset}.tar.gz" in audit_text
    assert ci_text.count(GITLEAKS_VERSION_DECLARATION) >= 1
    assert audit_text.count(GITLEAKS_VERSION_DECLARATION) == 2, (
        "the install step and the preflight in scheduled-audits.yml must both "
        "declare the pinned version, exactly as ci.yml does."
    )


def _gitleaks_installs(workflow: dict) -> dict[str, dict]:
    return {
        job_name: step
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if str(step.get("name", "")).startswith("Install gitleaks")
    }


def test_the_test_job_installs_the_secrets_jobs_gitleaks_on_hosted_runners() -> None:
    """KEW_REQUIRE_TOOLS fails the `test` job when gitleaks is missing, and a
    hosted runner starts without it, so the job installs the `secrets` job's
    pinned build. One copy of the pinned checksums per workflow job may not
    drift from the other: a different build is a different gate."""
    ci = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    installs = _gitleaks_installs(ci)
    assert set(installs) == {"secrets", "test"}
    secrets, test = installs["secrets"], installs["test"]
    assert test["run"] == secrets["run"]
    assert test["env"] == secrets["env"] == {"GITLEAKS_VERSION": "8.30.0"}
    assert test.get("if") == "runner.environment == 'github-hosted'"
    assert "continue-on-error" not in test
    names = [step.get("name") for step in ci["jobs"]["test"]["steps"]]
    assert names.index("Install gitleaks") < names.index("Test (pytest)")


_TOOLS_STEP = (
    "Preflight — install the tools KEW_REQUIRE_TOOLS needs on the hosted runner"
)


def _apt_packages(run: str) -> set[str]:
    packages: set[str] = set()
    for line in run.splitlines():
        words = line.split()
        if "apt-get" in words and "install" in words:
            after = words[words.index("install") + 1 :]
            packages |= {word for word in after if not word.startswith("-")}
    return packages


def test_the_hosted_test_job_installs_every_tool_the_suite_requires() -> None:
    """KEW_REQUIRE_TOOLS=1 fails a skip for a missing tool, and a hosted VM
    starts without some of REQUIRED_TOOLS (ripgrep, lsof). Every package that
    list names is installed on the hosted runner; a tool it pins outside the
    distribution has its own install step."""
    from support.required_tools import REQUIRED_TOOLS

    ci = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    job = ci["jobs"]["test"]
    assert (
        job["env"].get("KEW_REQUIRE_TOOLS", ci["env"].get("KEW_REQUIRE_TOOLS")) == "1"
    )
    steps = {step.get("name"): step for step in job["steps"]}
    tools = steps[_TOOLS_STEP]
    assert tools.get("if") == "runner.environment == 'github-hosted'"
    assert "continue-on-error" not in tools
    installed = _apt_packages(tools["run"])
    # The hosted image's package index is stale until refreshed; an install
    # before the update can fail on a package the mirror has since replaced.
    assert _apt_update_precedes_install(tools["run"])
    wanted = {package for package in REQUIRED_TOOLS.values() if package}
    assert wanted <= installed, f"not installed: {sorted(wanted - installed)}"
    for tool, package in REQUIRED_TOOLS.items():
        if package is None:
            assert f"Install {tool}" in steps, f"no step installs {tool}"
    names = list(steps)
    assert names.index(_TOOLS_STEP) < names.index("Test (pytest)")


def _apt_update_precedes_install(run: str) -> bool:
    lines = [line.split() for line in run.splitlines()]
    updates = [
        i for i, words in enumerate(lines) if "apt-get" in words and "update" in words
    ]
    installs = [
        i for i, words in enumerate(lines) if "apt-get" in words and "install" in words
    ]
    return bool(updates and installs) and min(updates) < min(installs)


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ("sudo apt-get update -qq\nsudo apt-get install -y jq\n", True),
        ("sudo apt-get install -y jq\nsudo apt-get update -qq\n", False),
        ("sudo apt-get install -y jq\n", False),
    ],
)
def test_the_update_check_reads_the_order(run: str, expected: bool) -> None:
    assert _apt_update_precedes_install(run) is expected


def test_the_apt_reader_sees_only_packages() -> None:
    run = (
        "sudo apt-get update -qq\n"
        "sudo apt-get install -y --no-install-recommends jq lsof\n"
    )
    assert _apt_packages(run) == {"jq", "lsof"}


@pytest.mark.policy
def test_self_hosted_is_confined_to_host_state_or_guarded_workflows() -> None:
    """Only two kinds of workflow may go self-hosted, and they are not the same.

    Enumerated rather than pattern-matched: each entry is a claim someone made
    on purpose. HOST_STATE_WORKFLOWS are self-hosted permanently, because the
    machine's own state is the subject of the job. GUARDED_TEMPORARY_WORKFLOWS
    are self-hosted for a reason that expires, and are expected to leave the
    set — which is why they are a separate set rather than three more entries
    in the first one.
    """
    found = {
        path.name
        for path in _workflow_paths()
        if any(
            _is_self_hosted(job)
            for job in (yaml.safe_load(path.read_text())["jobs"]).values()
        )
    }
    allowed = HOST_STATE_WORKFLOWS | GUARDED_TEMPORARY_WORKFLOWS
    assert found == allowed, (
        f"self-hosted workflows changed: {sorted(found)} vs the allowed "
        f"{sorted(allowed)}. Adding one puts a machine holding cross-project "
        "credentials behind a new trigger — justify it here, and check whether "
        "the trigger can be reached by a fork."
    )
    assert not (HOST_STATE_WORKFLOWS & GUARDED_TEMPORARY_WORKFLOWS), (
        "a workflow is listed as both permanently and temporarily self-hosted; "
        "the whole point of the second set is that its members are expected to "
        "leave it."
    )


@pytest.mark.policy
def test_self_hosted_checkouts_carry_the_shared_clone_heal() -> None:
    """Any self-hosted job that checks out the repo must heal a pruned clone.

    A persistent runner reuses one clone. A workflow that takes a NON-CONE
    sparse checkout (the retired trivy-runner job did) leaves that clone with
    an empty worktree for whatever runs next — and `actions/checkout`'s own
    `sparse-checkout disable` does not reliably undo it: the observed failure
    had core.sparseCheckout still true after checkout reported success.

    Pinned per JOB rather than per workflow, because the heal is only useful in
    the job that would trip over it, and a job added later would otherwise
    inherit the workflow's reputation without its protection.
    """
    for path in _workflow_paths():
        workflow_name = path.name
        data = yaml.safe_load(path.read_text())
        for job_name, job in data["jobs"].items():
            if not _is_self_hosted(job):
                continue
            steps = job.get("steps") or []
            checks_out = any(
                str(step.get("uses", "")).startswith("actions/checkout")
                for step in steps
            )
            if not checks_out:
                continue
            names = [step.get("name") for step in steps]
            assert SPARSE_HEAL_STEP in names, (
                f"{workflow_name} job {job_name!r} runs self-hosted and checks "
                f"out the repo, but has no {SPARSE_HEAL_STEP!r} step. A prior "
                "non-cone sparse checkout on that shared clone leaves the "
                "worktree empty, and the job fails with a bare 'No such file "
                "or directory' nowhere near the cause."
            )
            heal_at = names.index(SPARSE_HEAL_STEP)
            last_checkout = max(
                index
                for index, step in enumerate(steps)
                if str(step.get("uses", "")).startswith("actions/checkout")
            )
            # After the LAST checkout, not merely after A checkout: a second
            # checkout later in the job re-prunes the clone, and a heal that ran
            # before it healed a state nothing else would see.
            assert heal_at > last_checkout, (
                f"{workflow_name} job {job_name!r} runs the heal at step "
                f"{heal_at} but checks out again at {last_checkout}. The later "
                "checkout undoes it."
            )
            body = str(steps[heal_at].get("run", ""))
            for command in SPARSE_HEAL_COMMANDS:
                assert command in body, (
                    f"{workflow_name} job {job_name!r} has a step named "
                    f"{SPARSE_HEAL_STEP!r} that does not run {command!r}. The "
                    "name is not the heal — a step called this and running "
                    "`echo` satisfied the previous version of this assertion."
                )


@pytest.mark.parametrize(
    ("workflow", "job_name", "first_docker_step"),
    [
        ("ci.yml", "test", "Test (pytest)"),
        ("ci.yml", "agent-image-scan", "Agent image CVE gate"),
        ("release.yml", "serial-tests", "Verify serial test prerequisites"),
    ],
)
def test_self_hosted_docker_jobs_wait_on_runner_lock(
    workflow: str, job_name: str, first_docker_step: str
) -> None:
    data = yaml.safe_load((WORKFLOWS_DIR / workflow).read_text())
    job = data["jobs"][job_name]
    assert "concurrency" not in job, "GitHub concurrency cancels older pending jobs"
    assert job["runs-on"] == RUNNER_BY_EVENT_VISIBILITY
    # The lock path is fixed inside the script (/tmp), not taken from the job
    # environment, so two runner services with different TMPDIRs still contend
    # on one file.
    assert "TMPDIR" not in (job.get("env") or {})
    expected_concurrency = {
        "ci.yml": {
            # A labeled event for a label other than the CI approval runs no
            # job, so it gets its own group and cannot cancel a real run (#2033).
            "group": (
                "ci-${{ github.event.pull_request.number || github.ref }}"
                "${{ github.event.action == 'labeled' && "
                "github.event.label.name != 'ci-approved' && "
                "format('-label-{0}', github.run_id) || '' }}"
            ),
            "cancel-in-progress": True,
        },
        "release.yml": {"group": "release", "cancel-in-progress": False},
    }
    assert data["concurrency"] == expected_concurrency[workflow]
    steps = job["steps"]
    command = "python3 .github/scripts/docker_daemon_lock.py"
    acquire_command = (
        f"{command} acquire --timeout-seconds 5400 --hold-max-seconds 2700"
    )
    acquisitions = [
        i for i, step in enumerate(steps) if step.get("run") == acquire_command
    ]
    assert len(acquisitions) == 1, "each Docker job needs one blocking acquire"
    acquire_index = acquisitions[0]
    checkout_index = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert acquire_index > checkout_index
    docker_index = next(
        i for i, step in enumerate(steps) if step.get("name") == first_docker_step
    )
    # The lock is taken as late as possible: immediately before the first
    # Docker-using step, so checkout, preflight, and dependency installation
    # overlap with the other runner's Docker work instead of holding the daemon.
    assert acquire_index == docker_index - 1
    acquire = steps[acquire_index]
    # Skipped only on a GitHub-hosted VM, which runs one job and shares no
    # daemon. On the self-hosted runner the lock is unconditional.
    assert acquire.get("if") == "runner.environment == 'self-hosted'"
    assert "continue-on-error" not in acquire
    assert acquire["name"].startswith(INFRA_STEP_NAME_PREFIXES)
    release = steps[-1]
    release_command = f"{command} release --job-status ${{{{ job.status }}}}"
    assert release.get("run") == release_command
    assert release.get("if") == "always() && runner.environment == 'self-hosted'"
    assert release["name"].startswith(INFRA_STEP_NAME_PREFIXES)
    assert sum(step.get("run") == release_command for step in steps) == 1
    for step in (acquire, release):
        assert "working-directory" not in step
        assert "env" not in step, "both runners must use the same default lock path"

    # Step limits bound the entire critical section. Job time includes waiting.
    work = steps[acquire_index + 1 : -1]
    assert all(step.get("timeout-minutes", 0) > 0 for step in work)
    work_minutes = sum(step["timeout-minutes"] for step in work)
    assert work_minutes <= 40
    # Successful release takes at most one second. Failed release may wait
    # 900 seconds, while the holder's existing deadline bounds draining.
    assert release["timeout-minutes"] * 60 > 900
    assert 2 * (work_minutes * 60 + 1) < 5400
    assert acquire["timeout-minutes"] * 60 > 5400
    assert job["timeout-minutes"] > (
        acquire["timeout-minutes"] + work_minutes + release["timeout-minutes"]
    )
    # The holder's own bound exceeds the protected work but stays BELOW the
    # acquire timeout: an abandoned holder (worker died without the release
    # step) frees the daemon before any waiter times out (review round 2).
    hold_max_seconds = 2700
    assert work_minutes * 60 < hold_max_seconds < 5400


@pytest.mark.policy
def test_no_workflow_reuses_a_clone_it_did_not_create() -> None:
    """A non-cone sparse-checkout may only appear in a job nobody else follows.

    Replaces `test_scheduled_workflows_carry_sparse_heal`. That test pinned the
    *remedy* — self-heal steps in every job that might inherit a pruned clone —
    which was the right shape while many jobs shared one persistent clone.
    Portable jobs now use fresh hosted clones. Host jobs retain their repairs.

    What still needs guarding is the cause: `sparse-checkout-cone-mode: false`
    prunes the tree, and it is safe *only* because the job that does it runs
    alone on a disposable runner. Putting one on a self-hosted job would
    reintroduce #932/#938 silently — the failure lands in whatever workflow runs
    next, which is why it took two incidents to find the first time.
    """
    for path in _workflow_paths():
        data = yaml.safe_load(path.read_text())
        for job_name, job in data["jobs"].items():
            steps = job.get("steps", [])
            # Normalized, not compared to str(False). YAML gives a bool for
            # `false`, a string for `"false"`, and either case — so the old
            # `== "False"` matched exactly one spelling, and a raw-text
            # prefilter above it skipped the file entirely for the others.
            # Verified: `sparse-checkout-cone-mode: "False"` on a self-hosted
            # job passed.
            uses_sparse = any(
                str(step.get("with", {}).get("sparse-checkout-cone-mode", ""))
                .strip()
                .lower()
                == "false"
                for step in steps
                if isinstance(step, dict)
            )
            if not uses_sparse:
                continue
            assert not _is_self_hosted(job), (
                f"{path.name}:{job_name} runs a non-cone sparse-checkout on "
                f"{job.get('runs-on')!r}. On a persistent runner that prunes a "
                "clone other jobs reuse, and the failure surfaces in an "
                "unrelated workflow later (#932/#938). Either keep it hosted or "
                "restore an explicit self-heal on every job sharing that clone."
            )


def test_setup_node_preserves_no_github_cache() -> None:
    """#1752: setup-node's `cache:` post step uploads the runner's 10 GB ~/.npm,
    which previously caused 40–55 minute runs. Keep that input absent."""
    data = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    for job_name, job in data["jobs"].items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/setup-node"):
                assert "cache" not in (step.get("with") or {}), (
                    f"{job_name}: setup-node carries a cache: input (#1752)"
                )
                assert job.get("timeout-minutes", 0) > 0, (
                    f"{job_name}: a Node job needs timeout-minutes (#1752)"
                )


@pytest.mark.policy
def test_every_setup_uv_uses_a_job_private_runner_cache() -> None:
    expected = (
        "${{ runner.temp }}/kew-uv-${{ github.run_id }}-"
        "${{ github.run_attempt }}-${{ github.job }}"
    )
    setup_steps = []
    for path in _workflow_paths():
        data = yaml.safe_load(path.read_text())
        for job_name, job in data["jobs"].items():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    setup_steps.append((path.name, job_name, step))

    assert len(setup_steps) == 17
    for workflow, job, step in setup_steps:
        assert step.get("with", {}).get("cache-local-path") == expected, (
            f"{workflow}:{job} does not isolate setup-uv's writable cache"
        )


def test_verify_preserves_a_caller_owned_uv_cache() -> None:
    text = VERIFY_SH.read_text()
    assignments = re.findall(r"^(?:export\s+)?UV_CACHE_DIR=", text, re.MULTILINE)

    assert assignments == []
    assert not re.search(r"^\s*unset\s+.*\bUV_CACHE_DIR\b", text, re.MULTILINE)


@pytest.mark.serial
def test_private_cache_recipe_preserves_status_and_another_allocation(
    tmp_path: Path,
) -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text()
    start_marker = "<!-- private-uv-cache-recipe:start -->"
    end_marker = "<!-- private-uv-cache-recipe:end -->"
    assert start_marker in contributing and end_marker in contributing, (
        "the executable private-cache recipe was removed"
    )
    recipe = contributing.split(start_marker, 1)[1].split(end_marker, 1)[0]
    assert "```bash" in recipe, "the private-cache recipe lost its shell block"
    recipe = recipe.split("```bash", 1)[1].split("```", 1)[0]
    allocation_record = tmp_path / "allocation"
    other_cache = tmp_path / "kew-active" / "uv-cache"
    other_cache.mkdir(parents=True)
    other_cache.joinpath("active.bin").write_bytes(b"active")
    verify_line = 'UV_CACHE_DIR="$private_uv_root/uv-cache" bash scripts/verify.sh'
    probe_line = (
        'printf "%s" "$private_uv_root" > "$KEW_RECIPE_ALLOCATION"\n'
        'UV_CACHE_DIR="$private_uv_root/uv-cache" bash -c "exit 23"'
    )
    assert verify_line in recipe
    recipe = recipe.replace(verify_line, probe_line)

    result = subprocess.run(
        ["bash", "-c", recipe],
        env={
            **os.environ,
            "KEW_RECIPE_ALLOCATION": str(allocation_record),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23, result.stdout + result.stderr
    assert not Path(allocation_record.read_text()).exists()
    assert other_cache.joinpath("active.bin").read_bytes() == b"active"


def _write_cache_probe_wheel(path: Path) -> None:
    module = """\
import sys
import time
from pathlib import Path

def main():
    if len(sys.argv) == 1:
        from .late import VALUE
        print(VALUE)
        return
    ready, release, output = map(Path, sys.argv[1:])
    ready.write_text("ready")
    deadline = time.monotonic() + 10
    while not release.exists():
        if time.monotonic() >= deadline:
            raise SystemExit("release timeout")
        time.sleep(0.01)
    from .late import VALUE
    output.write_text(VALUE)
"""
    files = {
        "kew_cache_probe/__init__.py": module,
        "kew_cache_probe/late.py": 'VALUE = "late import succeeded"\n',
        "kew_cache_probe-1.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: kew-cache-probe\nVersion: 1.0\n"
        ),
        "kew_cache_probe-1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: kew-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "kew_cache_probe-1.0.dist-info/entry_points.txt": (
            "[console_scripts]\nkew-cache-probe = kew_cache_probe:main\n"
        ),
    }
    record = "".join(f"{name},,\n" for name in files)
    record += "kew_cache_probe-1.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as wheel:
        for name, contents in files.items():
            wheel.writestr(name, contents)
        wheel.writestr("kew_cache_probe-1.0.dist-info/RECORD", record)


@pytest.mark.serial
def test_private_cache_cleanup_preserves_an_active_uvx_environment(
    tmp_path: Path,
) -> None:
    uvx = shutil.which("uvx")
    assert uvx is not None, "uvx is required for the real cache-isolation proof"
    wheel = tmp_path / "kew_cache_probe-1.0-py3-none-any.whl"
    _write_cache_probe_wheel(wheel)
    task_a = tmp_path / "kew-task-a"
    task_b = tmp_path / "kew-task-b"
    cache_a = task_a / "uv-cache"
    cache_b = task_b / "uv-cache"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    output = tmp_path / "output"
    command = [uvx, "--offline", "--from", str(wheel), "kew-cache-probe"]
    env_a = {
        **os.environ,
        "UV_CACHE_DIR": str(cache_a),
        "UV_TOOL_BIN_DIR": str(task_a / "uv-bin"),
        "UV_TOOL_DIR": str(task_a / "uv-tools"),
    }
    env_b = {
        **os.environ,
        "UV_CACHE_DIR": str(cache_b),
        "UV_TOOL_BIN_DIR": str(task_b / "uv-bin"),
        "UV_TOOL_DIR": str(task_b / "uv-tools"),
    }
    active = subprocess.Popen(
        [*command, str(ready), str(release), str(output)],
        env=env_a,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            if active.poll() is not None:
                stdout, stderr = active.communicate()
                pytest.fail(f"active uvx exited early:\n{stdout}{stderr}")
            assert time.monotonic() < deadline, "active uvx did not start"
            time.sleep(0.01)

        completed = subprocess.run(
            command,
            env=env_b,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.strip() == "late import succeeded"
        shutil.rmtree(task_b)
        assert cache_a.is_dir()

        release.touch()
        stdout, stderr = active.communicate(timeout=10)
        assert active.returncode == 0, stdout + stderr
        assert output.read_text() == "late import succeeded"
    finally:
        if active.poll() is None:
            active.kill()
            active.wait()
