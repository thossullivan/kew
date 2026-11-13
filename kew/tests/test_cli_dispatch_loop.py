"""Plan 2 Task 4: `kew dispatch <issue> --loop` wiring + loop_style guard."""

import pytest
from typer.testing import CliRunner

from kew.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_loop_project(tmp_path, monkeypatch):
    """Keep managed ledger discovery out of the operator's repository."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))


def test_dispatch_loop_query_failure_exits_two(monkeypatch):
    from unittest.mock import MagicMock

    from kew.selector import GhQueryError

    _common_patches(monkeypatch)
    select = MagicMock(side_effect=GhQueryError("token expired"))
    driver = MagicMock()
    monkeypatch.setattr("kew.cli.select_next_issue", select)
    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.run", driver)
    result = runner.invoke(app, ["dispatch", "42", "--loop"])
    assert result.exit_code == 2, result.output
    assert "GitHub query failed" in result.output
    assert "token expired" in result.output
    driver.assert_not_called()


def _common_patches(
    monkeypatch,
    *,
    loop_style="native",
    captured=None,
    runner_name="claude-code",
    model="m",
    labels=None,
    file_model=None,
):
    from kew.config import DispatchConfig
    from kew.runners.registry import ResolvedRoute

    load_config_calls: list[dict] = []
    resolve_calls: list[dict] = []
    if captured is not None:
        captured["load_config_calls"] = load_config_calls
        captured["resolve_calls"] = resolve_calls

    def _tracking_load_config(store=None):
        load_config_calls.append({"store": store})
        config = DispatchConfig()
        # Loops run on the checkout escape hatch until #1844; canonical refuses.
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        if file_model is not None:
            # A DB override layer that disagrees with the file: the store-resolved
            # load sees `model`, the file-only load sees `file_model`. The route
            # the loop actually runs must come from the store-resolved one.
            config.agent.model = file_model if store is None else model
        return config

    monkeypatch.setattr("kew.cli.load_config", _tracking_load_config)
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a, **k: None)
    monkeypatch.setattr(
        "kew.cli.select_next_issue",
        lambda **k: {
            "number": 7,
            "title": "fix",
            "body": "do it",
            "labels": labels if labels is not None else [],
        },
    )

    _runner_name = runner_name
    _runner_style = loop_style

    class _Runner:
        name = _runner_name
        loop_style = _runner_style

    def _fake_resolve_runner(config, complexity=None):
        resolve_calls.append({"complexity": complexity})
        # Echo the config's model when a file/override split is under test, so the
        # route proves WHICH config it was resolved from.
        return ResolvedRoute(
            runner=_Runner(),
            model=config.agent.model if file_model is not None else model,
        )

    monkeypatch.setattr("kew.cli.resolve_runner", _fake_resolve_runner)

    def _fake_driver_run(self, loop, *, route=None):
        if captured is not None:
            captured["loop"] = loop
            captured["route"] = route
        loop.status = "achieved"
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.run", _fake_driver_run)


def test_dispatch_loop_native_runs_driver(monkeypatch):
    captured: dict = {}
    _common_patches(monkeypatch, loop_style="native", captured=captured)
    result = runner.invoke(app, ["dispatch", "7", "--loop"])
    assert result.exit_code == 0, result.output
    assert captured["loop"].issue_num == 7
    assert captured["loop"].source == "issue"
    assert captured["loop"].runner_name == "claude-code"
    assert any(c["store"] is not None for c in captured["load_config_calls"]), (
        "load_config must be called with store= inside _run_loop_dispatch"
    )


@pytest.mark.parametrize("output", ["failed", "#1946 refusal: printed by command"])
def test_dispatch_loop_executed_gate_failure_exits_one(monkeypatch, output):
    _common_patches(monkeypatch)
    reason = f"gate_failed: command exited 7: stdout: {output}; stderr: "

    def failed(self, loop, **kwargs):
        loop.status = "needs_human"
        loop.status_reason = reason
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.run", failed)
    result = runner.invoke(app, ["dispatch", "7", "--loop"])
    assert result.exit_code == 1, result.output
    assert reason in result.output


def test_dispatch_loop_unknown_issue_errors(monkeypatch):
    _common_patches(monkeypatch, loop_style="native")
    monkeypatch.setattr("kew.cli.select_next_issue", lambda **k: None)
    result = runner.invoke(app, ["dispatch", "7", "--loop"])
    assert result.exit_code != 0
    assert (
        "could not be loaded" in result.output.lower()
        or "not found" in result.output.lower()
    )


def test_dispatch_loop_routes_through_the_complexity_table(monkeypatch):
    """#1111: --loop must resolve its route through [routing], not [agent].

    A ``simple``-labelled issue routes to the simple tier's runner/model, and
    that same route is what the driver is handed (no second resolution that
    silently reverts to the agent default).
    """
    captured: dict = {}
    _common_patches(
        monkeypatch,
        captured=captured,
        runner_name="opencode",
        model="openrouter/deepseek/deepseek-v4-flash",
        labels=[{"name": "simple"}],
    )

    result = runner.invoke(app, ["dispatch", "7", "--loop"])

    assert result.exit_code == 0, result.output
    assert captured["resolve_calls"] == [{"complexity": "simple"}]
    assert captured["loop"].runner_name == "opencode"
    route = captured["route"]
    assert route is not None, "the routed route must be handed to the driver"
    assert route.runner.name == "opencode"
    assert route.model == "openrouter/deepseek/deepseek-v4-flash"


def test_dispatch_loop_route_resolves_from_the_effective_config(monkeypatch):
    """#1111 + O1/#892: a DB route override governs the loop's route.

    ``config_overrides`` rows for ``agent.model``/``routing.*`` (the #659 runtime
    surface the /config page writes) are applied by ``load_config(store=store)``.
    The loop path must resolve its route AFTER that re-resolve — resolving from
    the file-only config would hand the driver the stale committed route and
    silently ignore the override, which every other dispatch kind honours.
    """
    captured: dict = {}
    _common_patches(
        monkeypatch,
        captured=captured,
        runner_name="opencode",
        file_model="claude-opus-5",  # what the committed kew.toml says
        model="openrouter/deepseek/deepseek-v4-flash",  # what the override says
        labels=[{"name": "simple"}],
    )

    result = runner.invoke(app, ["dispatch", "7", "--loop"])

    assert result.exit_code == 0, result.output
    override_model = "openrouter/deepseek/deepseek-v4-flash"
    assert captured["route"].model == override_model
    # ...and the override route is what gets recorded on the loop, so the whole
    # life of the loop runs on it rather than the file value.
    assert captured["loop"].model == override_model
    assert override_model in result.output


def test_dispatch_loop_records_the_routed_complexity_tier(monkeypatch):
    """The tier the route was resolved at is recorded with it (#1111).

    A later re-resolve (a mid-flight route override on resume) needs the tier, or
    it lands on ``[routing].default`` — the escalation this issue removed.
    """
    captured: dict = {}
    _common_patches(monkeypatch, captured=captured, labels=[{"name": "simple"}])

    result = runner.invoke(app, ["dispatch", "7", "--loop"])

    assert result.exit_code == 0, result.output
    assert captured["loop"].complexity == "simple"


def test_dispatch_loop_fails_closed_on_a_non_native_routed_runner(monkeypatch):
    """A routed runner that can't drive its own loop must fail closed, loudly,
    before any loop record exists."""
    captured: dict = {}
    _common_patches(
        monkeypatch,
        loop_style="delegated",
        captured=captured,
        runner_name="not-native",
        labels=[{"name": "complex"}],
    )

    result = runner.invoke(app, ["dispatch", "7", "--loop"])

    assert result.exit_code != 0
    out = result.output.lower()
    assert "not-native" in out
    assert "native" in out
    assert "loop" not in captured, "no loop may be built for a non-native route"


@pytest.mark.parametrize(
    "status,launched,reason,expected",
    [
        ("needs_human", True, "run exited nonzero (137)", 1),
        ("budget_limited", True, "run ended: budget_exceeded", 1),
        ("needs_human", True, "verification failed", 1),
        ("needs_human", False, "no verify command configured", 2),
        ("budget_limited", False, "budget admission refused", 2),
    ],
)
def test_round_four_loop_terminal_exit(monkeypatch, status, launched, reason, expected):
    _common_patches(monkeypatch)

    def finish(self, loop, *, route):
        self.launched = launched
        loop.status = status
        loop.status_reason = reason
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.run", finish)
    result = runner.invoke(app, ["dispatch", "7", "--loop"])
    assert result.exit_code == expected, result.output
    assert status in result.output
    assert reason in result.output


@pytest.mark.parametrize("launched", [False, True])
def test_round_four_loop_driver_query_failure(monkeypatch, launched):
    from kew.selector import GhQueryError

    _common_patches(monkeypatch)

    def fail(self, loop, *, route):
        self.launched = launched
        raise GhQueryError("query unavailable")

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.run", fail)
    result = runner.invoke(app, ["dispatch", "7", "--loop"])
    assert result.exit_code == 2, result.output
    assert "query unavailable" in result.output


def test_round_ten_dispatch_loop_teardown_exit(monkeypatch, tmp_path):
    from test_loops_delegate import _round_ten_teardown_launch

    from kew.run_store import SqliteRunStore

    _common_patches(monkeypatch)
    monkeypatch.setattr("kew.cli._open_run_store", SqliteRunStore)
    for started in (True, False):

        def run(self, loop, *, route, started=started):
            return _round_ten_teardown_launch(self, loop, tmp_path, started)

        monkeypatch.setattr("kew.loops.delegate.DelegateDriver.run", run)
        result = runner.invoke(app, ["dispatch", "7", "--loop"])
        assert result.exit_code == (1 if started else 2), result.output
        assert (
            "sidecar teardown failed" if started else "pre-launch failure"
        ) in result.output
