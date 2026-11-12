"""The harness refuses silent skips and loose markers (#2480).

Each case runs a real pytest in a subprocess against kew's own
``pyproject.toml`` (``-c``), so what is pinned is the configuration and plugin
the suite actually runs under, not a copy. Outcomes are read from the junit
report that run writes, not from its terminal output.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET  # nosec B405 - our own pytest's junit
from pathlib import Path

import pytest
from support import required_tools, source_ast

KEW_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = KEW_DIR / "tests"

_MISSING_TOOL = "frobnicate-cli is not installed"
_INTENDED = "live Docker containment tests require KEW_RUN_LIVE_DOCKER=1"


def _run(
    tmp_path: Path, source: str, *, require_tools: str | None
) -> tuple[int, dict[str, tuple[str, str]], str]:
    """Run ``source`` as a test module; return (exit, {name: (outcome, text)})."""
    module = tmp_path / "test_synthetic.py"
    module.write_text(textwrap.dedent(source))
    junit = tmp_path / "junit.xml"
    env = dict(os.environ)
    env.pop(required_tools.ENV, None)
    if require_tools is not None:
        env[required_tools.ENV] = require_tools
    env["PYTHONPATH"] = os.pathsep.join(
        [str(TESTS_DIR), *filter(None, [env.get("PYTHONPATH")])]
    )
    proc = subprocess.run(  # nosec B603 - this interpreter's pytest
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(KEW_DIR / "pyproject.toml"),
            "-p",
            "no:cacheprovider",
            "-p",
            "support.required_tools",
            f"--junitxml={junit}",
            str(module),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    cases: dict[str, tuple[str, str]] = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):  # nosec B314
            outcome, text = "passed", ""
            for tag in ("failure", "error", "skipped"):
                found = case.find(tag)
                if found is not None:
                    outcome = tag
                    text = f"{found.get('message', '')}\n{found.text or ''}"
                    break
            cases[case.get("name", "")] = (outcome, text)
    return proc.returncode, cases, proc.stdout + proc.stderr


_SKIPS = f"""
    import pytest

    def test_skips_in_the_body():
        pytest.skip({_MISSING_TOOL!r})

    @pytest.mark.skipif(True, reason={_MISSING_TOOL!r})
    def test_skips_by_mark():
        pass

    def test_skips_on_purpose():
        pytest.skip({_INTENDED!r})

    @pytest.mark.xfail(reason="known bug", strict=False)
    def test_expected_failure():
        assert False
"""


def test_a_missing_tool_skip_fails_naming_the_tool(tmp_path: Path) -> None:
    code, cases, output = _run(tmp_path, _SKIPS, require_tools="1")

    assert code == 1, output
    for name in ("test_skips_in_the_body", "test_skips_by_mark"):
        outcome, text = cases[name]
        assert outcome in ("failure", "error"), (name, cases)
        assert _MISSING_TOOL in text, text
        assert f"{required_tools.ENV}=1" in text, text


def test_an_intended_skip_and_an_xfail_still_skip_under_the_mode(
    tmp_path: Path,
) -> None:
    # Without the missing-tool cases, so the exit code is about these two
    # alone. A refused xfail changes neither the exit code (pytest does not
    # count a report carrying ``wasxfail`` as failed) nor junit ("skipped"),
    # only its outcome, so the outcome count is what is asserted.
    source = f"""
        import pytest

        def test_skips_on_purpose():
            pytest.skip({_INTENDED!r})

        @pytest.mark.xfail(reason="known bug", strict=False)
        def test_expected_failure():
            assert False
    """
    code, cases, output = _run(tmp_path, source, require_tools="1")

    assert code == pytest.ExitCode.OK, output
    assert "1 skipped, 1 xfailed" in output, output
    assert cases["test_skips_on_purpose"][0] == "skipped", cases
    assert cases["test_expected_failure"][0] == "skipped", cases


@pytest.mark.parametrize("require_tools", [None, ""])
def test_without_the_mode_every_skip_skips(
    tmp_path: Path, require_tools: str | None
) -> None:
    code, cases, output = _run(tmp_path, _SKIPS, require_tools=require_tools)

    assert code == 0, output
    assert {name: outcome for name, (outcome, _) in cases.items()} == {
        "test_skips_in_the_body": "skipped",
        "test_skips_by_mark": "skipped",
        "test_skips_on_purpose": "skipped",
        "test_expected_failure": "skipped",
    }


def test_a_module_level_skip_fails_at_collection(tmp_path: Path) -> None:
    source = """
        import pytest

        pytest.importorskip("frobnicate_module_that_is_not_installed")

        def test_never_reached():
            pass
    """
    code, _, output = _run(tmp_path, source, require_tools="1")
    assert code != 0, output
    assert "frobnicate_module_that_is_not_installed" in output, output

    code, _, output = _run(tmp_path, source, require_tools=None)
    assert code == pytest.ExitCode.NO_TESTS_COLLECTED, output
    assert "1 skipped" in output, output


def test_any_other_value_is_a_usage_error(tmp_path: Path) -> None:
    code, _, output = _run(tmp_path, _SKIPS, require_tools="yes")

    assert code == pytest.ExitCode.USAGE_ERROR, output
    assert f"{required_tools.ENV} must be unset, empty or 1" in output, output


def test_an_unregistered_marker_fails_collection(tmp_path: Path) -> None:
    source = """
        import pytest

        @pytest.mark.serail
        def test_misspelled_marker():
            pass
    """
    code, cases, output = _run(tmp_path, source, require_tools=None)

    assert code == pytest.ExitCode.INTERRUPTED, output
    assert "'serail' not found in `markers` configuration option" in output, output
    assert "test_misspelled_marker" not in cases


def test_an_unexpectedly_passing_xfail_fails(tmp_path: Path) -> None:
    source = """
        import pytest

        @pytest.mark.xfail(reason="was broken once")
        def test_fixed_since():
            assert True
    """
    code, cases, output = _run(tmp_path, source, require_tools=None)

    assert code == pytest.ExitCode.TESTS_FAILED, output
    outcome, text = cases["test_fixed_since"]
    assert outcome == "failure", cases
    assert "XPASS(strict)" in text, text


def test_this_session_runs_under_the_plugin(request: pytest.FixtureRequest) -> None:
    import conftest

    plugin = request.config.pluginmanager.get_plugin(
        conftest.REQUIRED_TOOLS_PLUGIN_NAME
    )
    assert plugin is required_tools


def test_every_intended_skip_says_why() -> None:
    for pattern, why in required_tools.INTENTIONAL_SKIPS:
        assert why.strip(), pattern


@pytest.mark.parametrize(
    "reason",
    [
        "docker not available",
        "docker daemon not reachable",
        "gitleaks is not installed",
        "needs git",
        "needs bash",
        "jq not available",
        "lsof is not installed",
        "the producer needs bash, git, jq, sha256sum, as it does in the image",
        "needs docker for the KEW_TEST_AGENT_IMAGE live half",
        "not a git checkout; nothing to scan",
        "frontend/src/app.css not present in this checkout",
    ],
)
def test_no_intended_skip_covers_a_missing_tool(reason: str) -> None:
    assert not required_tools.intended(reason)


def _unconditional_skip_reasons() -> list[tuple[str, str]]:
    """``(where, reason)`` for every ``@pytest.mark.skip(reason=...)`` decorator."""
    found = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                if source_ast.dotted(deco.func) != "pytest.mark.skip":
                    continue
                reason = source_ast.keyword(deco, "reason") or next(
                    iter(deco.args), None
                )
                text = reason.value if isinstance(reason, ast.Constant) else ""
                where = f"{path.relative_to(KEW_DIR)}:{deco.lineno}"
                found.append((where, str(text)))
    return found


@pytest.mark.policy
def test_every_unconditional_skip_in_the_suite_is_declared() -> None:
    # A bare ``@pytest.mark.skip`` skips in every environment, so under
    # KEW_REQUIRE_TOOLS=1 (CI, verify.sh) an undeclared one fails the gate
    # everywhere; catch it here, in any targeted round, instead.
    undeclared = [
        (where, reason)
        for where, reason in _unconditional_skip_reasons()
        if not required_tools.intended(reason)
    ]
    assert undeclared == [], undeclared
