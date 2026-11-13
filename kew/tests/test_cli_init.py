"""`kew init` over fixture repositories (#2502).

Detection that would reach this machine — ``gh``, the container runtime, the
runner CLIs on PATH — is pinned per test, as are ``kew setup`` and the doctor
checks, so each test sees only the fixture repository's own files. Of the
doctor checks only ``runner-credential`` runs, since it reads the environment
alone.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew import onboarding
from kew.cli import app, doctor
from kew.cli import init as init_cmd
from kew.config import DispatchConfig, load_config
from kew.readiness import CheckResult, CheckStatus

runner = CliRunner()

_REAL_RUN_SETUP = init_cmd._run_setup

_CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def _credential_check_only(config: DispatchConfig) -> list[CheckResult]:
    """Doctor's real ``runner-credential`` check, with every other one left out."""
    groups = [
        (ids, check)
        for ids, check in doctor._check_groups(config)
        if ids == ("runner-credential",)
    ]
    with patch.object(doctor, "_check_groups", lambda _config: groups):
        results = doctor.run_checks(config)
    return [result for result in results if result.id == "runner-credential"]


@pytest.fixture
def setup_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        "kew.cli.init._run_setup", lambda: calls.append("setup") or True
    )
    return calls


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup_calls: list[str]
) -> Path:
    """An empty fixture project with every machine-dependent probe pinned."""
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(onboarding, "detect_repo", lambda root: "acme/widgets")
    monkeypatch.setattr(onboarding, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(onboarding, "detect_runners", lambda: ())
    monkeypatch.setattr("kew.cli.init.run_checks", _credential_check_only)
    return tmp_path


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A person on stdin: the scripted ``input=`` stands in for their answers."""
    monkeypatch.setattr(init_cmd, "_stdin_is_tty", lambda: True)


def _init(*args: str):
    return runner.invoke(app, ["init", *args])


def _written(repo: Path) -> dict:
    return tomllib.loads((repo / "kew.toml").read_text())


# --- AC1: detection -------------------------------------------------------


def test_detect_justfile_test_recipe_becomes_the_ci_command(
    repo: Path, setup_calls: list[str]
) -> None:
    (repo / "justfile").write_text(
        'set shell := ["bash", "-c"]\n\nlint:\n    ruff check .\n\n'
        "test *ARGS: lint\n    pytest {{ARGS}}\n"
    )

    result = _init("--yes", "--runner", "codex")

    assert result.exit_code == 0, result.output
    config = load_config(repo / "kew.toml", env={})
    assert config.commands.ci == "just test"
    assert config.agent.runner == "codex"
    assert config.project.repo == "acme/widgets"
    assert setup_calls == ["setup"]


def test_detect_commands_reads_each_supported_file(tmp_path: Path) -> None:
    make = tmp_path / "make"
    make.mkdir()
    (make / "Makefile").write_text(
        ".PHONY: ci\nFLAGS := -q\nci: deps\n\tpytest\nfmt:\n\truff format .\n"
    )
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "format": "prettier -w ."}})
    )
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\nline-length = 88\n"
    )
    (py / "uv.lock").write_text("")
    empty = tmp_path / "empty"
    empty.mkdir()

    assert onboarding.detect_commands(make) == ("make ci", "make fmt")
    assert onboarding.detect_commands(npm) == ("npm test", "npm run format")
    assert onboarding.detect_commands(py) == ("uv run pytest", "uv run ruff format .")
    assert onboarding.detect_commands(empty) == (None, None)


def test_detect_justfile_assignment_is_not_a_recipe(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("test := 'x'\n")

    assert onboarding.detect_commands(tmp_path) == (None, None)


def test_detect_installed_runner_is_chosen_without_a_flag(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboarding, "detect_runners", lambda: ("codex", "pi"))

    result = _init("--yes")

    assert result.exit_code == 0, result.output
    assert _written(repo)["agent"]["runner"] == "codex"


def test_detect_flags_win_over_detection(repo: Path) -> None:
    (repo / "justfile").write_text("test:\n    pytest\n")

    result = _init(
        "--yes",
        "--runner",
        "codex",
        "--ci",
        "tox",
        "--format",
        "black .",
        "--runtime",
        "podman",
        "--model",
        "gpt-5.6-sol",
        "--budget-per-issue",
        "40",
    )

    assert result.exit_code == 0, result.output
    assert _written(repo) == {
        "project": {"repo": "acme/widgets"},
        "agent": {"runner": "codex", "model": "gpt-5.6-sol"},
        "commands": {"ci": "tox", "format": "black ."},
        "sandbox": {"runtime": "podman"},
        "budget": {"per_issue_usd": 40.0},
    }


def test_detect_unknown_runner_writes_nothing(repo: Path) -> None:
    result = _init("--yes", "--runner", "nope")

    assert result.exit_code == 2
    assert "Unknown runner 'nope'" in result.output
    assert not (repo / "kew.toml").exists()


def test_detect_invalid_value_is_refused_before_writing(repo: Path) -> None:
    result = _init("--yes", "--runner", "codex", "--runtime", "lxc")

    assert result.exit_code == 2
    assert "invalid kew.toml" in result.output
    assert not (repo / "kew.toml").exists()


def test_detect_declined_confirmation_writes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, terminal: None
) -> None:
    _fully_detectable(repo, monkeypatch)
    result = runner.invoke(app, ["init", "--runner", "codex"], input="n\n")

    assert result.exit_code == 2
    assert not (repo / "kew.toml").exists()


# --- AC2: posture -----------------------------------------------------------


def test_posture_claude_code_without_credential_still_writes_and_names_the_fix(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _CREDENTIALS:
        monkeypatch.delenv(name, raising=False)

    result = _init("--yes", "--runner", "claude-code")

    assert (repo / "kew.toml").is_file()
    assert _written(repo)["agent"]["runner"] == "claude-code"
    assert result.exit_code == 1, result.output
    assert "runner-credential: FAIL" in result.output
    assert "fix: export ANTHROPIC_API_KEY=<key>" in result.output
    assert "issue(s) to fix: runner-credential" in result.output


def test_posture_claude_code_with_credential_passes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")

    result = _init("--yes", "--runner", "claude-code")

    assert result.exit_code == 0, result.output
    assert "runner-credential: PASS" in result.output
    assert "kew run --dry-run" in result.output


def test_posture_native_codex_needs_no_environment_credential(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _CREDENTIALS:
        monkeypatch.delenv(name, raising=False)

    result = _init("--yes", "--runner", "codex")

    assert result.exit_code == 0, result.output
    assert "runner-credential: skipped (codex: native posture" in result.output
    assert "fix: export" not in result.output


def test_posture_runner_credential_is_reported_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Init reports doctor's credential row, not a second one of its own."""
    monkeypatch.setattr("kew.cli.init.run_checks", doctor.run_checks)
    for name in _CREDENTIALS:
        monkeypatch.delenv(name, raising=False)

    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = _init("--yes", "--runner", "claude-code")

    assert result.output.count("runner-credential: FAIL") == 1, result.output
    assert result.output.count("fix: export ANTHROPIC_API_KEY=<key>") == 1


def test_posture_doctor_fixes_are_carried_into_the_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = CheckResult(
        "route-image",
        CheckStatus.FAIL,
        "image kew-agent-pi:x is not built",
        fix="kew sandbox build --runner pi",
    )
    monkeypatch.setattr("kew.cli.init.run_checks", lambda config: [image])
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "not-real")

    result = _init("--yes", "--runner", "pi")

    assert result.exit_code == 1
    assert (repo / "kew.toml").is_file()
    assert "fix: kew sandbox build --runner pi" in result.output
    assert "1 issue(s) to fix: route-image" in result.output


# --- AC3: overwrite ---------------------------------------------------------


def test_overwrite_refused_leaves_existing_file_byte_identical(
    repo: Path, setup_calls: list[str]
) -> None:
    original = b"# hand-written\r\n[project]\nname = 'mine'\n"
    (repo / "kew.toml").write_bytes(original)

    result = _init("--yes", "--runner", "codex")

    assert result.exit_code == 2
    assert "--force" in result.output
    assert (repo / "kew.toml").read_bytes() == original
    assert setup_calls == []


def test_overwrite_with_force_replaces_the_file(repo: Path) -> None:
    (repo / "kew.toml").write_text("[project]\nname = 'mine'\n")

    result = _init("--yes", "--force", "--runner", "codex")

    assert result.exit_code == 0, result.output
    assert _written(repo)["agent"] == {"runner": "codex"}


# --- AC4: minimal -----------------------------------------------------------


def test_minimal_file_holds_only_values_that_differ_from_defaults(
    repo: Path,
) -> None:
    (repo / "Makefile").write_text("test:\n\tpytest\n")

    result = _init(
        "--yes",
        "--runner",
        "codex",
        "--runtime",
        "docker",
        "--budget-per-issue",
        "15",
    )

    assert result.exit_code == 0, result.output
    written = _written(repo)
    assert written == {
        "project": {"repo": "acme/widgets"},
        "agent": {"runner": "codex"},
    }
    defaults = DispatchConfig()
    for section, fields in written.items():
        for name, value in fields.items():
            assert getattr(getattr(defaults, section), name) != value, (section, name)


def test_minimal_values_drops_every_default() -> None:
    defaults = DispatchConfig()
    every_default = {
        key: getattr(getattr(defaults, key[0]), key[1])
        for key in onboarding.INIT_FIELDS
    }

    assert onboarding.minimal_values(every_default) == {}


def test_posture_setup_that_exits_on_a_missing_tool_still_reports_the_checks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def setup_without_gh(**_: object) -> None:
        raise SystemExit(2)  # what preflight_tools does when gh is absent

    monkeypatch.setattr(init_cmd, "_run_setup", _REAL_RUN_SETUP)
    monkeypatch.setattr("kew.cli.setup", setup_without_gh)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")

    result = _init("--yes", "--runner", "claude-code")

    assert (repo / "kew.toml").is_file()
    assert result.exit_code == 1, result.output
    assert "runner-credential: PASS" in result.output
    assert "1 issue(s) to fix: setup" in result.output


def test_detect_unparseable_files_are_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "package.json").write_text("{not json")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest\n")

    with caplog.at_level("WARNING", logger="kew.onboarding"):
        assert onboarding.detect_commands(tmp_path) == (None, None)

    assert "unparseable package.json" in caplog.text
    assert "unparseable pyproject.toml" in caplog.text


# --- #2503: interactive ----------------------------------------------------


def _fully_detectable(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "justfile").write_text("test:\n    pytest\nfmt:\n    ruff format .\n")
    monkeypatch.setattr(onboarding, "detect_runners", lambda: ("codex",))


def test_interactive_minimal_asks_only_for_the_runner(
    repo: Path, monkeypatch: pytest.MonkeyPatch, terminal: None
) -> None:
    _fully_detectable(repo, monkeypatch)

    # Exactly two answers: the runner, then the write confirmation. Any further
    # question would read EOF and abort, so a zero exit proves none was asked.
    result = runner.invoke(app, ["init"], input="\ny\n")

    assert result.exit_code == 0, result.output
    assert "Runner [codex]:" in result.output
    for question in init_cmd._QUESTIONS.values():
        assert question not in result.output
    assert "what it costs" in result.output
    assert "lemonade" in result.output  # every runner is in the posture table
    assert _written(repo) == {
        "project": {"repo": "acme/widgets"},
        "agent": {"runner": "codex"},
        "commands": {"ci": "just test", "format": "just fmt"},
    }


def test_interactive_asks_for_each_undetected_value_with_its_default(
    repo: Path, monkeypatch: pytest.MonkeyPatch, terminal: None
) -> None:
    monkeypatch.setattr(onboarding, "detect_repo", lambda root: None)
    monkeypatch.setattr(onboarding, "detect_runtime", lambda: None)

    result = runner.invoke(
        app, ["init"], input="acme/gadgets\ntox\n\npodman\nnope\ncodex\ny\n"
    )

    assert result.exit_code == 0, result.output
    assert f"{init_cmd._QUESTIONS[('commands', 'ci')]} [make test]:" in result.output
    assert f"{init_cmd._QUESTIONS[('sandbox', 'runtime')]} [docker]:" in result.output
    assert "Error: choose one of: claude-code, codex" in result.output
    assert _written(repo) == {
        "project": {"repo": "acme/gadgets"},
        "agent": {"runner": "codex"},
        "commands": {"ci": "tox"},
        "sandbox": {"runtime": "podman"},
    }


def test_interactive_flags_are_not_asked_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch, terminal: None
) -> None:
    monkeypatch.setattr(onboarding, "detect_repo", lambda root: None)

    result = runner.invoke(
        app,
        [
            "init",
            "--repo",
            "acme/x",
            "--runner",
            "codex",
            "--ci",
            "tox",
            "--format",
            "",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "Runner" not in result.output
    assert _written(repo)["project"] == {"repo": "acme/x"}


def _image_check(status: CheckStatus) -> CheckResult:
    return CheckResult(
        "agent-image",
        status,
        "image is not built" if status is CheckStatus.FAIL else "image is built",
        fix="kew sandbox build --runner claude-code",
    )


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        "kew.cli.sandbox_build", lambda **kw: calls.append(kw["runner"])
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    return calls


def test_interactive_decline_image_build_leaves_the_check_failing_with_its_fix(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: None,
    builds: list[str],
) -> None:
    _fully_detectable(repo, monkeypatch)
    monkeypatch.setattr(
        "kew.cli.init.run_checks", lambda config: [_image_check(CheckStatus.FAIL)]
    )

    result = runner.invoke(app, ["init"], input="claude-code\ny\nn\n")

    assert builds == []
    assert result.exit_code == 1, result.output
    assert "Run `kew sandbox build --runner claude-code` now?" in result.output
    closing = result.output.split("now?", 1)[1]
    assert "agent-image: FAIL" in closing
    assert "fix: kew sandbox build --runner claude-code" in closing
    assert "issue(s) to fix: agent-image" in closing


def test_interactive_accepted_image_build_runs_and_the_checks_rerun(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: None,
    builds: list[str],
) -> None:
    _fully_detectable(repo, monkeypatch)

    def checks(config: object) -> list[CheckResult]:
        status = CheckStatus.OK if builds else CheckStatus.FAIL
        return [_image_check(status)]

    monkeypatch.setattr("kew.cli.init.run_checks", checks)

    result = runner.invoke(app, ["init"], input="claude-code\ny\ny\n")

    assert builds == ["claude-code"]
    assert result.exit_code == 0, result.output
    assert "agent-image: PASS" in result.output
    assert "First command to run: kew run --dry-run" in result.output


def test_interactive_offers_the_network_and_credential_guidance(
    repo: Path, monkeypatch: pytest.MonkeyPatch, terminal: None
) -> None:
    _fully_detectable(repo, monkeypatch)
    for name in _CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    network = CheckResult(
        "allowlist-network",
        CheckStatus.FAIL,
        "network is missing",
        fix="kew sandbox network",
    )
    monkeypatch.setattr(
        "kew.cli.init.run_checks",
        lambda config: [network, *_credential_check_only(config)],
    )
    created: list[bool] = []
    monkeypatch.setattr(
        "kew.cli.sandbox_network", lambda **kw: created.append(kw["check"])
    )

    result = runner.invoke(app, ["init"], input="claude-code\ny\ny\n")

    assert created == [False]
    assert "claude setup-token" in result.output
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.output
    assert result.exit_code == 1


def test_interactive_replay_command_reproduces_the_same_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch, terminal: None
) -> None:
    monkeypatch.setattr(onboarding, "detect_repo", lambda root: None)
    monkeypatch.setattr(onboarding, "detect_runtime", lambda: None)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "not-real")

    result = runner.invoke(
        app,
        ["init", "--budget-per-issue", "12.5", "--model", "m 1"],
        input="acme/gadgets\ntox -e py\n\npodman\npi\ny\n",
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"^.*?(kew init --yes .*)$", result.output, re.MULTILINE)
    assert match is not None, result.output
    replay = shlex.split(match.group(1))
    first = (repo / "kew.toml").read_bytes()
    assert b"podman" in first and b"tox -e py" in first

    # The replay must not lean on detection: the next machine detects otherwise.
    (repo / "kew.toml").unlink()
    (repo / "justfile").write_text("test:\n    pytest\nfmt:\n    black .\n")
    monkeypatch.setattr(onboarding, "detect_repo", lambda root: "other/repo")
    monkeypatch.setattr(onboarding, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(onboarding, "detect_runners", lambda: ("codex",))
    monkeypatch.setattr(init_cmd, "_stdin_is_tty", lambda: False)

    again = runner.invoke(app, replay[1:])

    assert again.exit_code == 0, again.output
    assert (repo / "kew.toml").read_bytes() == first


@pytest.mark.parametrize(
    ("args", "named", "not_named"),
    [
        ([], ("--yes", "--runner", "--ci", "--format"), ("--repo", "--runtime")),
        (["--runner", "codex", "--ci", "tox"], ("--yes", "--format"), ("--runner",)),
    ],
)
def test_non_interactive_no_tty_without_yes_exits_2_naming_the_missing_flags(
    repo: Path,
    setup_calls: list[str],
    args: list[str],
    named: tuple[str, ...],
    not_named: tuple[str, ...],
) -> None:
    result = _init(*args)

    assert result.exit_code == 2, result.output
    assert "not a terminal" in result.output
    for flag in named:
        assert flag in result.output
    for flag in not_named:
        assert flag not in result.output.split("pass --yes", 1)[1]
    assert not (repo / "kew.toml").exists()
    assert setup_calls == []
