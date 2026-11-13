"""``KEW_REQUIRE_TOOLS=1``: a skip the environment caused fails the run (#2480).

Most skips in this suite are a missing tool — docker, git, bash, gitleaks,
jq — or a platform that cannot host the check. That is right on a laptop
without docker, and wrong on a gate: the publication, scan-gate and containment
suites skip when a tool is missing, pytest still exits 0, and a misprovisioned
runner passes them having proved nothing. CI and ``scripts/verify.sh`` set the
variable, so there a skip is a failure that names what was missing.

A skip that is not about the environment — an opt-in live round, a parameter
the test does not apply to, a rule with nothing to check today — is declared
in :data:`INTENTIONAL_SKIPS`, the one place the mode reads, with why. Anything
else fails. A new skip therefore fails the gate until someone either
provisions what it names or writes down here why skipping is the intent.

Registered by ``tests/conftest.py`` like the shard plugin, and pinned by
``tests/test_required_tools_mode.py``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Generator

import pytest

ENV = "KEW_REQUIRE_TOOLS"

#: ``(pattern, why)``: a skip whose reason matches ``pattern`` (``re.search``)
#: is intended and still skips under ``KEW_REQUIRE_TOOLS=1``. Keep each pattern
#: as narrow as the reason it admits, so it cannot cover a missing tool.
INTENTIONAL_SKIPS: tuple[tuple[str, str], ...] = (
    (
        r"^every pilot module waits through support/tui_wait\.py",
        "test_serial_phase.py: the rule has no module to check today; it is "
        "pinned on synthetic sources by a sibling test",
    ),
    (
        r"set KEW_LIVE_OPENSHELL=1",
        "live OpenShell rounds dial an operator gateway; never run in CI",
    ),
    (
        r"^live Docker containment tests require KEW_RUN_LIVE_DOCKER=1$",
        "opt-in live containment round; once opted in, missing docker says so",
    ),
    (
        r"^opt-in: set KEW_TEST_AGENT_IMAGE ",
        "opt-in live publication half against a locally built image; once "
        "opted in, missing docker says so",
    ),
    (
        r"^set KEW_WRITE_WORK_VIEW_GOLDEN=1 to regenerate",
        "golden regeneration, not a check",
    ),
    (
        r"^(codex|hermes) fails closed without a model: ",
        "test_runner_container_args.py: the parameter does not apply to a "
        "runner that refuses a model-less launch",
    ),
    (
        r"^this fixture (has no preserved artifact|offers an executable verb)$",
        "test_surface_convergence.py: the parameter does not apply to that fixture",
    ),
    (
        r"^filesystem is case-sensitive: ",
        "the case-folding probe only means something on a case-insensitive "
        "filesystem; the platform, not a tool",
    ),
    (
        r"^no /proc on this host$",
        "test_openshell_launch.py (the agent-exec probe) and "
        "test_openshell_launcher.py (the exec marker): each runs a Linux "
        "script that only ever runs inside kew's Linux sandbox container and "
        "reads /proc; a host with none (macOS) has nothing for it to read; "
        "the platform, not a tool",
    ),
    (
        r"^B\d+ of the test-integrity epic \(#2477\) has not landed: ",
        "test_dispatch_to_merge_scenarios.py: a scenario written ahead of the "
        "feature it asserts; nothing observable exists until that node lands",
    ),
    (
        r"^root (ignores|traverses|reads|can list) ",
        "running as root makes a permission check vacuous; the user, not a tool",
    ),
)

# Not here, because it never skips: ``requires_switchyard_image`` tests are
# *deselected* in tests/conftest.py, with the reason printed in the header and
# the summary, and deselection is not a skip this mode sees.

_ENABLED = pytest.StashKey[bool]()


def enabled_from_env() -> bool:
    """Whether the mode is on; any value but unset, empty or ``1`` is refused."""
    value = os.environ.get(ENV, "")
    if value not in ("", "1"):
        raise pytest.UsageError(f"{ENV} must be unset, empty or 1, got {value!r}")
    return value == "1"


def intended(reason: str) -> bool:
    """Whether a skip reason is declared in :data:`INTENTIONAL_SKIPS`."""
    return any(re.search(pattern, reason) for pattern, _ in INTENTIONAL_SKIPS)


def _skip_reason(longrepr: object) -> str:
    # A skip report's longrepr is (path, lineno, "Skipped: <reason>").
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


def _where(longrepr: object) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return f"{longrepr[0]}:{longrepr[1]}"
    return "<unknown>"


def _refuse(report: pytest.TestReport | pytest.CollectReport) -> None:
    reason = _skip_reason(report.longrepr)
    if intended(reason):
        return
    where = _where(report.longrepr)
    report.outcome = "failed"
    report.longrepr = (
        f"{ENV}=1: this skip is the environment, not the test: {reason}\n"
        f"  skipped at {where}\n"
        "Provision what it names, or, if skipping is the intent, declare the "
        "reason with why in INTENTIONAL_SKIPS (tests/support/required_tools.py)."
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_ENABLED] = enabled_from_env()


def pytest_report_header(config: pytest.Config) -> str | None:
    if not config.stash.get(_ENABLED, False):
        return None
    return f"{ENV}=1: a skip the environment caused fails the run"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    # An xfail is reported as a skip carrying ``wasxfail``; it is not one.
    if (
        report.skipped
        and not hasattr(report, "wasxfail")
        and item.config.stash.get(_ENABLED, False)
    ):
        _refuse(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    # A module-level ``pytest.importorskip`` or ``pytest.skip`` skips here.
    report = yield
    if report.skipped and collector.config.stash.get(_ENABLED, False):
        _refuse(report)
    return report


# --- the verify host's tools (#2763) ----------------------------------------
# A test that finds one of these absent skips; under KEW_REQUIRE_TOOLS=1 that
# skip fails the run, so kew's own verify-host recipe
# (kew/src/kew/hosts/provision/ubuntu-24.04.sh) must install every one.
# tests/test_hosts_provision.py holds the recipe to this list, and holds every
# ``shutil.which(...)`` in tests/ to this list or HOST_PROVIDED_TOOLS, so a new
# tool lookup cannot leave the recipe behind unnoticed — a lookup whose name it
# cannot read off the source (a literal, a literal loop, a parametrization or
# a module constant) fails that test rather than escaping it (#2867).

#: Executable a test looks up with ``shutil.which`` → the Ubuntu package that
#: installs it, or ``None`` for one the recipe installs from a pinned release
#: rather than the distribution. Each names a test that skips without it.
REQUIRED_TOOLS: dict[str, str | None] = {
    # tests/test_bounded_run_keys.py::
    #   test_round16_fixture_values_fail_generic_api_key_grep
    "rg": "ripgrep",
    # tests/test_security.py::TestHookExecutionBash::
    #   test_program_flag_deny_set_is_derived_from_the_real_interpreter[ruby]
    "ruby": "ruby",
    # tests/test_security.py::TestHookExecutionBash::
    #   test_program_flag_deny_set_is_derived_from_the_real_interpreter[perl]
    "perl": "perl",
    # tests/test_security.py (_REQUIRES_JQ), tests/test_sandbox.py
    "jq": "jq",
    # tests/test_docker_daemon_lock.py::
    #   test_lsof_host_warnings_do_not_block_the_drain
    "lsof": "lsof",
    # tests/test_hosts_providers.py::
    #   test_ssh_resolves_the_boundary_with_nothing_to_send
    "ssh": "openssh-client",
    # tests/test_gitleaks_allowlist.py, tests/test_bounded_run_keys.py::
    #   test_round16_metadata_allowlist_preserves_detection — and
    #   scripts/verify.sh exits without it. Pinned: the version is the gate.
    "gitleaks": None,
}

#: Executable a test looks up that the recipe does not install from this list,
#: with where a verify host gets it. Kept as narrow as REQUIRED_TOOLS: a name
#: here is a claim about the host, not a way to silence the policy test.
HOST_PROVIDED_TOOLS: dict[str, str] = {
    "git": "the recipe's base packages and its self-check",
    "docker": "the recipe's docker step and its self-check",
    "node": "the recipe's node step and its self-check",
    "bash": "Ubuntu's base system; the recipe itself runs in it",
    "sh": "Ubuntu's base system (dash)",
    "cat": "coreutils, in Ubuntu's base system",
    "chmod": "coreutils, in Ubuntu's base system",
    "mktemp": "coreutils, in Ubuntu's base system",
    "rm": "coreutils, in Ubuntu's base system",
    "tar": "tar, in Ubuntu's base system",
    "gzip": "gzip, in Ubuntu's base system",
    "sleep": "coreutils, in Ubuntu's base system",
    "true": "coreutils, in Ubuntu's base system",
    "timeout": "coreutils, in Ubuntu's base system",
    "realpath": "coreutils, in Ubuntu's base system",
    "sha256sum": "coreutils, in Ubuntu's base system",
    "printenv": "coreutils, in Ubuntu's base system",
    "mkdir": "coreutils, in Ubuntu's base system",
    "env": "coreutils, in Ubuntu's base system",
    "id": "coreutils, in Ubuntu's base system",
    "uv": "the recipe's uv step and its self-check",
    # Looked up to stand one in where a developer's machine (macOS) has none.
    "setsid": "util-linux, in Ubuntu's base system",
    "gtimeout": "macOS name for timeout; looked up only as its fallback",
    "gsha256sum": "macOS name for sha256sum; looked up only as its fallback",
    "ps": "procps, in Ubuntu's base system",
    "uvx": "ships with uv, which the recipe installs and self-checks",
    "lint-imports": "the project's dev dependencies, installed by uv sync",
    "shellcheck": "the project's dev dependencies (shellcheck-py)",
}
