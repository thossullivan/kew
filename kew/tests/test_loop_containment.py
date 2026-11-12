"""#1977: a loop's agent launch is contained or explicitly admitted to the host.

A loop launches its agent through ``run_tracked_agent`` with no contained
image. Without ``[sandbox] dangerously_skip_containment = true`` the default
claude-code route must refuse before any checkout, agent work or spend, exit 2,
and leave the same pre-launch refusal evidence as a refused remediation.

This module is exempt from the suite-wide uncontained default in conftest, so
the real loop admission answers for these default configs.
"""

from __future__ import annotations

import pytest
from test_loops_delegate import (
    _FakeGate,
    _loop,
    _loops_use_canonical_publication,  # noqa: F401 -- autouse publication stub
    _patch_run,
)
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig
from kew.loops.delegate import DelegateDriver
from kew.loops.verification import GateVerdict
from kew.run_store import SqliteRunStore
from kew.runners import resolve_runner
from kew.runners.registry import ResolvedRoute
from kew.tracking import DispatchResult

_FLAG = "dangerously_skip_containment = true"


def _default_route() -> ResolvedRoute:
    """The default route's runner, with a priced model so pricing cannot refuse."""
    route = resolve_runner(DispatchConfig())
    assert route.runner.name == "claude-code"
    return ResolvedRoute(runner=route.runner, model="claude-sonnet-4-6")


def _passing_gate() -> _FakeGate:
    return _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))


@pytest.fixture
def launch_calls(monkeypatch, tmp_path) -> dict:
    """Record checkout and agent launch; admit the real claude-code runner.

    The imported autouse fixture stubs canonical publication, so without the
    #1977 gate a default loop goes on to its checkout and ``run_tracked_agent``.
    """
    from kew import worktree

    captured = _patch_run(monkeypatch, tmp_path)
    calls: dict = {"worktree": [], "agent": captured}
    generation = worktree.write_checkout_generation(tmp_path)

    def prepare(loop, config, **kwargs):
        calls["worktree"].append(loop.loop_id)
        kwargs["binding"].checkout_generation = generation
        return tmp_path

    def no_preflight_failure(config: DispatchConfig, resolved: ResolvedRoute) -> None:
        assert resolved.runner.name == "claude-code"
        return None

    monkeypatch.setattr("kew.loops.delegate._prepare_worktree", prepare)
    monkeypatch.setattr(
        "kew.loops.delegate._resolved_runner_preflight_failure", no_preflight_failure
    )
    return calls


def test_default_claude_code_loop_refuses_before_checkout_or_agent(
    launch_calls, capsys
):
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)

    finished = DelegateDriver(
        config=DispatchConfig(), store=store, gate=_passing_gate()
    ).run(loop, route=_default_route())

    assert launch_calls["agent"] == {}, "run_tracked_agent must not be reached"
    assert launch_calls["worktree"] == []
    assert finished.status == "needs_human"
    assert finished.status_reason is not None
    assert _FLAG in finished.status_reason
    assert _FLAG in capsys.readouterr().out
    [posture] = store.list_audit_events(event="sandbox_resolved")
    assert posture.payload is not None
    assert posture.payload["posture"] == "refused"
    assert posture.payload["runner"] == "claude-code"
    [completed] = store.list_audit_events(event="dispatch_completed")
    assert completed.payload is not None
    assert completed.payload["exit_code"] == 2
    assert completed.payload["error_category"] == "containment_refused"
    row = store.get(str(posture.run_id))
    assert row is not None
    assert (row.status, row.exit_code, row.run_kind) == ("refused", 2, "loop")


def test_default_claude_code_resume_refuses_before_checkout(launch_calls):
    store = SqliteRunStore(":memory:")
    loop = _loop()
    loop.status = "needs_human"
    loop.model = _default_route().model
    store.save_loop(loop)

    with pytest.raises(ValueError, match=_FLAG):
        DelegateDriver(
            config=DispatchConfig(), store=store, gate=_passing_gate()
        ).resume(loop, "go on")

    assert launch_calls["agent"] == {}
    [completed] = store.list_audit_events(event="dispatch_completed")
    assert completed.payload is not None
    assert completed.payload["exit_code"] == 2


def test_dispatch_loop_without_flag_exits_two_naming_it(
    launch_calls, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))

    def load_config(store: object = None) -> DispatchConfig:
        config = DispatchConfig()
        config.agent.model = "claude-sonnet-4-6"  # priced, so pricing cannot refuse
        return config

    def gate(config: DispatchConfig) -> _FakeGate:
        assert config.sandbox.dangerously_skip_containment is False
        return _passing_gate()

    def select_issue(*, config: DispatchConfig, force_issue: int) -> dict:
        assert force_issue == 7
        return {"number": 7, "title": "fix", "body": "do it", "labels": []}

    monkeypatch.setattr("kew.cli.load_config", load_config)
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.select_next_issue", select_issue)
    monkeypatch.setattr("kew.cli._open_run_store", SqliteRunStore)
    monkeypatch.setattr("kew.loops.delegate._build_default_gate", gate)

    result = CliRunner().invoke(app, ["dispatch", "7", "--loop"])

    assert launch_calls["agent"] == {}, "run_tracked_agent must not be reached"
    assert launch_calls["worktree"] == []
    assert result.exit_code == 2, result.output
    assert _FLAG in result.output


def test_flag_admits_host_loop_and_records_uncontained_posture(
    monkeypatch, tmp_path, capsys
):
    from kew import worktree

    launches: list[object] = []

    def agent_process(*, contained: object = None, **_unchecked: object):
        launches.append(contained)
        return DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            error_category="",
        )

    (tmp_path / ".git").mkdir(exist_ok=True)
    generation = worktree.write_checkout_generation(tmp_path)

    def prepare(loop, config, **kwargs):
        kwargs["binding"].checkout_generation = generation
        return tmp_path

    def no_preflight_failure(config: DispatchConfig, resolved: ResolvedRoute) -> None:
        assert resolved.runner.name == "claude-code"
        return None

    monkeypatch.setattr("kew.loops.delegate._prepare_worktree", prepare)
    monkeypatch.setattr(
        "kew.loops.delegate._resolved_runner_preflight_failure", no_preflight_failure
    )
    monkeypatch.setattr("kew.loops.delegate.build_repo_map", lambda *_a, **_k: "")
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **_k: "PROMPT")
    monkeypatch.setattr("kew.dispatch_core.run_agent", agent_process)
    # Checkout publication keeps host git out of this launch-posture test.
    config = DispatchConfig(
        sandbox={"dangerously_skip_containment": True},
        publication={"publish": "checkout"},
    )
    config.retry.max_attempts = 1
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    loop = _loop()
    store.save_loop(loop)

    DelegateDriver(config=config, store=store, gate=_passing_gate()).run(
        loop, route=_default_route()
    )

    assert launches == [None], "the admitted loop agent runs on the host"

    out = capsys.readouterr()
    assert "DANGEROUS: containment skipped" in out.out + out.err
    assert "first-class loops have no contained launch" in out.out + out.err
    [posture] = store.list_audit_events(event="sandbox_resolved")
    assert posture.payload is not None
    assert posture.payload["posture"] == "uncontained"
    assert posture.payload["admission"] == "dangerously_skip_containment"
    row = store.get(str(posture.run_id))
    assert row is not None
    assert row.run_kind == "loop"


def test_native_runner_loop_needs_no_flag() -> None:
    from kew.loops.delegate import resolve_loop_agent_posture

    assert resolve_loop_agent_posture(DispatchConfig(), runner="codex") == "native"
    with pytest.raises(RuntimeError, match=_FLAG):
        resolve_loop_agent_posture(DispatchConfig(), runner="claude-code")
    assert (
        resolve_loop_agent_posture(
            DispatchConfig(sandbox={"dangerously_skip_containment": True}),
            runner="claude-code",
        )
        == "uncontained"
    )
