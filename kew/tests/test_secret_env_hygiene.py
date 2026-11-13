"""A test failure must never be able to print an operator secret (#2197).

The container launcher hands the *process* environment to the child, so a mock
whose ``assert_called_once_with`` fails prints that environment verbatim in its
"Actual call" line. On 2026-09-29 a genuine failure in
``tests/test_sandbox.py::TestContainerHandle`` therefore wrote the operator's
live ``LINEAR_API_KEY`` into a full-gate pytest log.

The fix is in ``tests/conftest.py``: known secret variables are removed from
``os.environ`` for the whole run. This proves it end to end — a real pytest
subprocess, started from a parent that holds a sentinel secret, running a real
failing launch assertion — instead of only unit-testing the predicate.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest
from conftest import is_secret_env_name

_TESTS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _TESTS_DIR.parent

# Held by the parent, and secret-looking, so the guard must remove it.
_SECRET_SENTINEL = "LINEAR_API_KEY"
_SECRET_VALUE = "kew-2197-sentinel-must-not-be-printed"
# Held by the parent and NOT secret-looking, so it must survive and be printed.
# Without this control the test would pass just as well if the failure printed
# no environment at all.
_PLAIN_SENTINEL = "KEW_2197_PLAIN_MARKER"
_PLAIN_VALUE = "kew-2197-marker-expected-in-output"

_FAILING_LAUNCH_TEST = """
import subprocess
from unittest.mock import ANY, MagicMock, patch

from kew.sandbox import build_run_argv, launch_container


class _Stdout:
    def readline(self, limit=None):
        return ""


def test_a_launch_assertion_that_fails(tmp_path):
    argv = build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=tmp_path,
        slug="42",
        network="allowlist",
        runner="claude-code",
    )
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = _Stdout()
    proc.returncode = 0
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    no_container = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    with (
        patch("kew.sandbox.subprocess_utils.popen", return_value=proc) as popen,
        patch("kew.sandbox.subprocess_utils.run", return_value=no_container),
    ):
        assert launch_container(argv).wait() == 0
    # Deliberately wrong argv: the failure prints the call the mock really got,
    # including the environment the launcher passed through.
    popen.assert_called_once_with(
        ["a-deliberately-wrong-argv"],
        env=ANY,
        credential=None,
        stderr=subprocess.STDOUT,
    )
"""


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("LINEAR_API_KEY", True),
        ("GITHUB_TOKEN", True),
        ("GH_TOKEN", True),
        ("ANTHROPIC_API_KEY", True),
        ("OPENROUTER_PROVISIONING_KEY", True),
        ("CLAUDE_CODE_OAUTH_TOKEN", True),
        ("KEW_SERVE_TOKEN", True),
        ("SOME_VENDOR_SECRET", True),
        ("PATH", False),
        ("HOME", False),
        ("SSH_AUTH_SOCK", False),
        ("KEW_LOG_DIR", False),
    ],
)
def test_the_secret_name_predicate_covers_the_documented_classes(
    name: str, secret: bool
) -> None:
    assert is_secret_env_name(name) is secret


def test_the_suite_starts_with_no_secret_variables_in_the_environment() -> None:
    assert [name for name in os.environ if is_secret_env_name(name)] == []


#: A variable the live round sweeps for — claude-code declares it — so the
#: opt-in holds it back; ``_SECRET_SENTINEL`` is secret-looking but swept for
#: by nothing, so even the opt-in must not keep it (#2638).
_SWEPT_SENTINEL = "ANTHROPIC_API_KEY"
_SWEPT_VALUE = "kew-2638-swept-sentinel"


def test_an_ordinary_run_keeps_no_snapshot_of_what_it_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal must mean removal: no copy survives an ordinary run.

    The live-round hold below is the one exception to this module's rule, so
    the rule itself needs pinning: without the opt-in, the scrub deletes the
    variable and keeps nothing, and the only thing a failure can print is still
    a fixture value.
    """
    monkeypatch.setattr(conftest, "_held_for_live_round", {})
    monkeypatch.delenv(conftest._LIVE_ROUND_OPT_IN, raising=False)
    monkeypatch.setenv(_SECRET_SENTINEL, _SECRET_VALUE)
    monkeypatch.setenv(_SWEPT_SENTINEL, _SWEPT_VALUE)

    removed = conftest._scrub_secret_env()

    assert {_SECRET_SENTINEL, _SWEPT_SENTINEL} <= set(removed)
    assert _SECRET_SENTINEL not in os.environ
    assert conftest._held_for_live_round == {}


def test_the_live_round_opt_in_holds_only_what_the_round_sweeps_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AC7 round's leak sweep needs the value the scrub took away — no more.

    It asserts that no real credential reached the sandbox it created, and the
    operator's own credential is the only real value it can look for — but the
    scrub had already deleted exactly the names the runner declares, so the
    sweep ran over an empty set (PR #2520 review). The hold is what gives it
    something to look for. It once held every secret-looking variable the
    operator's shell exported, for the whole session (#2638); it holds the
    names the round sweeps for and nothing wider.
    """
    from kew.sandbox import runner_credential_env_vars

    assert _SWEPT_SENTINEL in runner_credential_env_vars("claude-code")
    monkeypatch.setattr(conftest, "_held_for_live_round", {})
    monkeypatch.setenv(conftest._LIVE_ROUND_OPT_IN, "1")
    monkeypatch.setenv(_SWEPT_SENTINEL, _SWEPT_VALUE)
    monkeypatch.setenv(_SECRET_SENTINEL, _SECRET_VALUE)
    monkeypatch.setenv(_PLAIN_SENTINEL, _PLAIN_VALUE)

    conftest._scrub_secret_env()

    assert _SWEPT_SENTINEL not in os.environ
    assert _SECRET_SENTINEL not in os.environ
    # Removed, but swept for by nothing: not held, even on a live run.
    assert conftest._held_for_live_round == {_SWEPT_SENTINEL: _SWEPT_VALUE}


@pytest.fixture
def _held_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[list[dict[str, str]]]:
    """Hold one value for the live round; after teardown, check it is gone.

    Requested BEFORE ``live_round_credentials``, so it is torn down AFTER it:
    the mapping the test was handed is checked once the fixture has finished.
    """
    monkeypatch.setattr(
        conftest, "_held_for_live_round", {_SWEPT_SENTINEL: _SWEPT_VALUE}
    )
    handed: list[dict[str, str]] = []
    yield handed
    assert handed, "the test never recorded what the fixture handed it"
    assert handed[0] == {}, "the live-round fixture kept its copy past the test"


def test_the_live_round_fixture_takes_the_values_out_of_conftest(
    _held_sentinel: list[dict[str, str]], live_round_credentials: dict[str, str]
) -> None:
    """The fixture is the one way to the values, and using it consumes them.

    A module-level accessor any module could import and call kept the
    operator's credentials readable for the whole session (#2638). Now the
    fixture moves them out of ``conftest`` as the round starts, a second
    request gets nothing, and the copy it handed over is cleared when the round
    ends — checked by ``_held_sentinel``'s teardown.
    """
    _held_sentinel.append(live_round_credentials)

    assert live_round_credentials == {_SWEPT_SENTINEL: _SWEPT_VALUE}
    assert conftest._held_for_live_round == {}
    assert not hasattr(conftest, "live_round_scrubbed_credentials")


@pytest.mark.parametrize("requested", [True, False], ids=["requested", "unrequested"])
def test_the_hold_is_dropped_when_no_collected_test_will_take_it(
    monkeypatch: pytest.MonkeyPatch, requested: bool
) -> None:
    """A live-opted run that deselects the round holds nothing past collection.

    And whatever survives collection is dropped when the session finishes,
    whether or not the round ran.
    """
    held = {_SWEPT_SENTINEL: _SWEPT_VALUE}
    monkeypatch.setattr(conftest, "_held_for_live_round", held)
    fixtures = ["tmp_path", conftest._LIVE_ROUND_FIXTURE] if requested else []
    session = SimpleNamespace(
        items=[SimpleNamespace(fixturenames=fixtures), SimpleNamespace()]
    )

    conftest.pytest_collection_finish(session)  # type: ignore[arg-type]

    assert held == ({_SWEPT_SENTINEL: _SWEPT_VALUE} if requested else {})

    conftest.pytest_sessionfinish(session)  # type: ignore[arg-type]

    assert held == {}


def test_a_failing_launch_assertion_cannot_print_a_parent_secret(
    tmp_path: Path,
) -> None:
    """A real pytest run, from a parent holding a secret, must not echo it."""
    child = tmp_path / "test_forced_launch_failure.py"
    child.write_text(_FAILING_LAUNCH_TEST, encoding="utf-8")

    environment = dict(os.environ)
    environment[_SECRET_SENTINEL] = _SECRET_VALUE
    environment[_PLAIN_SENTINEL] = _PLAIN_VALUE
    # The suite's own conftest is loaded as a plugin: the child file lives
    # outside tests/, so directory discovery would not find it, and the point
    # is to exercise the real guard rather than a copy of it.
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(_TESTS_DIR), str(_PROJECT_DIR / "src")]
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, a nested pytest run
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "conftest",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-q",
            str(child),
        ],
        cwd=_PROJECT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    # The failure really did print the launch environment...
    assert _PLAIN_VALUE in output, output
    # ...and the operator's secret was not in it.
    assert _SECRET_VALUE not in output
    assert _SECRET_SENTINEL not in output
