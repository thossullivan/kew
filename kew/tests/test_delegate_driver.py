"""Plan 2 Task 3: DelegateDriver — one supervised session, steps -> iterations."""

import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from kew.config import DispatchConfig
from kew.loops.delegate import (
    DelegateDriver,
    build_loop_from_issue,
    host_publication_route_rejection,
)
from kew.loops.verification import GateVerdict
from kew.publication import PublicationError
from kew.publication_canonical import CanonicalPublication
from kew.run_store import LoopRecord, SqliteRunStore
from kew.tracking import DispatchResult
from kew.worktree import (
    CheckoutUnclaimed,
    claim_checkout,
    read_checkout_generation,
    release_checkout_claim,
    require_checkout_claim,
    write_checkout_generation,
)


@pytest.fixture(autouse=True)
def _allow_driver_preflights(monkeypatch, tmp_path):
    """Supply checkout metadata and bypass runner checks in driver unit tests."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )


def test_host_publication_route_requires_canonical_publication():
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    rejection = host_publication_route_rejection(
        ResolvedRoute(CodexRunner(), "gpt-5.6-sol"), "first-class loops"
    )
    assert rejection is not None
    assert "v1 has no host publication broker" in rejection
    assert "sandbox" not in rejection
    assert (
        host_publication_route_rejection(
            ResolvedRoute(CodexRunner(), "gpt-5.6-sol"),
            "first-class loops",
            publish="canonical",
        )
        is None
    )


class _FakeGate:
    def __init__(self, verdict: GateVerdict) -> None:
        self._verdict = verdict
        self.calls: list[dict[str, object]] = []

    def evaluate(
        self,
        *,
        worktree,
        base_ref,
        changed_paths,
        agent_written_checkout=False,
        dangerously_skip_containment=False,
        published_sha=None,
        publication_base_sha=None,
        publication_remote=None,
        extra_guarded_paths=(),
    ) -> GateVerdict:
        assert agent_written_checkout is True
        assert published_sha is not None
        self.calls.append(
            {"worktree": worktree, "base_ref": base_ref, "changed_paths": changed_paths}
        )
        return self._verdict


def _write_event_stream(event_file: Path) -> None:
    """Two usage-bearing steps + a finished line, as the orchestrator writes them.

    Each step line carries a real ``ts`` (the orchestrator's ``_write_event``
    prepends it) so the driver can use per-step timestamps for ``started_at``.
    """
    lines = [
        {
            "ts": 1000.0,
            "type": "started",
            "runner": "claude-code",
            "model": "claude-sonnet-4-6",
            "issue": 7,
        },
        {"ts": 1010.0, "type": "step", "input": 100, "output": 20, "cost_usd": 0.01},
        {"ts": 1025.0, "type": "step", "input": 200, "output": 40, "cost_usd": 0.02},
        {
            "ts": 1030.0,
            "type": "finished",
            "exit_code": 0,
            "total_tokens": 360,
            "cost_usd": 0.03,
        },
    ]
    event_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _loop() -> LoopRecord:
    return LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="fix #7",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        issue_num=7,
        repo="thossullivan/kew",
        budget_usd=5.0,
        wall_clock_secs=600,
        max_steps=200,
    )


def _patch_run(
    monkeypatch,
    tmp_path,
    *,
    error_category="",
    model="claude-sonnet-4-6",
    exit_code=None,
) -> Path:
    """Stub run_tracked_agent to write an event stream and return a result."""
    from kew.runners.registry import ResolvedRoute

    event_file = tmp_path / "run.events.jsonl"
    _write_event_stream(event_file)
    eff_exit = exit_code if exit_code is not None else (0 if not error_category else 1)

    def _fake_run_tracked_agent(**kwargs):
        # Driver must pass loop_id + budget_override_usd + a wall-clock override.
        assert kwargs["loop_id"] == "L1"
        assert kwargs["budget_override_usd"] == 5.0
        assert kwargs["timeout_override_minutes"] == 10  # ceil(600/60)
        return DispatchResult(
            exit_code=eff_exit,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category=error_category,
            run_id="real-run-id",  # threaded into IterationRecord.run_id
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run_tracked_agent)

    # Resolve to a priced model so the unpriced-model fail-closed check passes.
    class _Runner:
        name = "claude-code"
        loop_style = "native"

    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda config, complexity=None: ResolvedRoute(runner=_Runner(), model=model),
    )
    # Avoid real worktree/runner/diff/prompt machinery in the unit test.
    monkeypatch.setattr(
        "kew.loops.delegate._prepare_worktree",
        lambda loop, config, **_kwargs: tmp_path,
    )
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")
    return event_file


def test_driver_records_one_iteration_per_step(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="3 tests green", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.run(loop)

    its = store.list_iterations("L1")
    assert len(its) == 2
    assert [it.idx for it in its] == [0, 1]
    assert all(it.kind == "assistant_turn" for it in its)
    assert its[0].tokens == 120  # 100 + 20
    assert its[1].cost_usd == 0.02
    # run_id is the REAL id from run_tracked_agent (valid FK), not a fabricated uuid.
    assert all(it.run_id == "real-run-id" for it in its)
    # started_at comes from each step's own ts, not a single now().
    assert its[0].started_at != its[1].started_at
    assert result.status == "achieved"
    assert result.status_reason == "3 tests green"


def _launch_with_bound_checkout(checkout, store, run_agent, /, **kwargs):
    require_checkout_claim(checkout)
    [event] = store.list_audit_events(event="loop_checkout_bound", limit=None)
    assert event.payload["checkout_id"] == read_checkout_generation(checkout)
    assert event.run_id == kwargs["run_id"]
    kwargs["paid_launch_check"]()
    return run_agent(**kwargs)


@pytest.mark.parametrize("existing_generation", [False, True])
def test_fresh_seam_captures_generation_under_lease(
    monkeypatch, tmp_path, existing_generation
):
    from kew.loops import delegate

    _patch_run(monkeypatch, tmp_path)
    before = write_checkout_generation(tmp_path) if existing_generation else ""
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    launch = Mock(
        side_effect=partial(
            _launch_with_bound_checkout, tmp_path, store, delegate.run_tracked_agent
        )
    )
    monkeypatch.setattr(delegate, "run_tracked_agent", launch)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))

    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)

    assert result.status == "achieved"
    launch.assert_called_once()
    generation = read_checkout_generation(tmp_path)
    assert generation
    if existing_generation:
        assert generation == before
    with pytest.raises(CheckoutUnclaimed):
        require_checkout_claim(tmp_path)


def _prepare_replaced_checkout(checkout, _loop, _config, **kwargs):
    claim_checkout(checkout)
    kwargs["binding"].checkout_generation = write_checkout_generation(checkout)
    write_checkout_generation(checkout)
    return checkout


def test_fresh_seam_refuses_a_generation_changed_after_preparation(
    monkeypatch, tmp_path
):
    from kew import dispatch_core
    from kew.loops import delegate

    _patch_run(monkeypatch, tmp_path)
    monkeypatch.setattr(
        delegate, "_prepare_worktree", partial(_prepare_replaced_checkout, tmp_path)
    )
    launch = Mock()
    monkeypatch.setattr(delegate, "run_tracked_agent", launch)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    with pytest.raises(
        dispatch_core._CheckoutIdentityRefused, match="identity changed"
    ):
        driver.run(loop)

    launch.assert_not_called()
    assert store.list_audit_events(event="loop_checkout_bound", limit=None) == []
    assert store.get_loop(loop.loop_id).status == "needs_human"
    with pytest.raises(CheckoutUnclaimed):
        require_checkout_claim(tmp_path)


def test_driver_routes_sabotage_to_needs_human(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(
        GateVerdict(passed=False, evidence="diff touched verify.sh", sabotage=True)
    )
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.run(loop)
    assert result.status == "needs_human"
    assert "verify.sh" in result.status_reason


def test_driver_routes_gate_failure_to_needs_human(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=False, evidence="2 tests red", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.run(loop)
    assert result.status == "needs_human"
    assert result.status_reason == "2 tests red"


def test_driver_persists_terminal_status_and_step_count(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)

    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.status == "achieved"
    assert persisted.step_count == 2
    assert persisted.finished_at is not None


def test_driver_budget_exceeded_maps_to_budget_limited(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path, error_category="budget_exceeded")
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    # Gate would pass, but the budget cap tripped first.
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
    assert result.status == "budget_limited"


def test_build_loop_from_issue_uses_config_defaults():
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.agent.timeout_minutes = 10
    config.agent.max_budget_usd = 4.0
    issue = {"number": 7, "title": "fix", "body": "do it"}
    loop = build_loop_from_issue(issue, config, runner_name="claude-code")
    assert loop.source == "issue"
    assert loop.issue_num == 7
    assert loop.runner_name == "claude-code"
    assert (
        loop.budget_usd == 4.0
    )  # config.agent.max_budget_usd, never None (validator > 0)
    assert loop.wall_clock_secs == 600  # 10 minutes
    assert loop.status == "pursuing"


def test_build_loop_from_issue_sets_item_id_from_github_number():
    from kew.config import DispatchConfig

    config = DispatchConfig()
    issue = {"number": 7, "title": "fix", "body": "do it"}
    loop = build_loop_from_issue(issue, config, runner_name="claude-code")
    assert loop.issue_num == 7
    assert loop.item_id == "7"


def test_build_loop_from_issue_preserves_opaque_item_id():
    """A non-numeric tracker id (e.g. "SFD-12") has no integer issue_num, but
    its opaque id must ride through as item_id — not be dropped to None."""
    from kew.config import DispatchConfig

    config = DispatchConfig()
    issue = {"number": "SFD-12", "title": "fix", "body": "do it"}
    loop = build_loop_from_issue(issue, config, runner_name="claude-code")
    assert loop.issue_num is None
    assert loop.item_id == "SFD-12"


def test_build_loop_from_issue_scales_max_steps_by_complexity():
    from kew.config import DispatchConfig

    config = DispatchConfig()
    issue = {"number": 1, "title": "t", "body": "b"}
    simple = build_loop_from_issue(
        issue, config, runner_name="claude-code", complexity="simple"
    )
    medium = build_loop_from_issue(
        issue, config, runner_name="claude-code", complexity="medium"
    )
    complex_ = build_loop_from_issue(
        issue, config, runner_name="claude-code", complexity="complex"
    )
    assert simple.max_steps == 80
    assert medium.max_steps == 200
    assert complex_.max_steps == 400


def test_build_loop_from_issue_defaults_to_medium_max_steps():
    from kew.config import DispatchConfig

    config = DispatchConfig()
    issue = {"number": 1, "title": "t", "body": "b"}
    loop = build_loop_from_issue(issue, config, runner_name="claude-code")
    assert loop.max_steps == 200


def test_max_steps_for_complexity():
    from kew.loops.delegate import max_steps_for_complexity

    assert max_steps_for_complexity("simple") == 80
    assert max_steps_for_complexity("medium") == 200
    assert max_steps_for_complexity("complex") == 400
    assert max_steps_for_complexity("unknown") == 200


def test_build_loop_from_issue_dispatch_time_max_steps_override():
    """--max-steps at dispatch time overrides the complexity default."""
    from kew.config import DispatchConfig

    config = DispatchConfig()
    issue = {"number": 1, "title": "t", "body": "b"}
    loop = build_loop_from_issue(
        issue,
        config,
        runner_name="claude-code",
        complexity="simple",
        max_steps_override=500,
    )
    assert loop.max_steps == 500


def test_driver_refuses_unpriced_model(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    # Resolve to a model NOT in MODEL_PRICING -> fail closed, never run unbounded.
    _patch_run(monkeypatch, tmp_path, model="some-unknown-model-9")
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
    assert result.status == "needs_human"
    assert "pricing table" in result.status_reason
    # It must NOT have launched the agent or recorded iterations.
    assert store.list_iterations("L1") == []


def test_driver_exceeding_max_steps_routes_to_needs_human(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    loop.max_steps = 1  # the event stream has 2 steps -> over cap
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
    assert result.status == "needs_human"
    assert "max_steps" in result.status_reason


def test_driver_unmapped_error_category_fails_closed(monkeypatch, tmp_path):
    """A crashed run with an unmapped error_category must NOT be gated to achieved.

    github_unreachable (and any other unknown category) produces no diff, so a
    gate that passes on the unchanged worktree must not stamp the loop achieved.
    The fail-closed rule: non-empty + not in _ERROR_STATUS -> needs_human, gate
    is never called.
    """
    from kew.config import DispatchConfig

    for category in ("github_unreachable", "exception", "github_auth"):
        _patch_run(monkeypatch, tmp_path, error_category=category)
        store = SqliteRunStore(":memory:")
        loop = _loop()
        store.save_loop(loop)
        # Gate would return passed=True — but it must never be consulted.
        gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
        result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
        assert result.status == "needs_human", (
            f"expected needs_human for error_category={category!r},"
            f" got {result.status!r}"
        )
        assert category in result.status_reason
        # The gate must not have been called — crashed run, no diff to evaluate.
        assert gate.calls == [], (
            f"gate was called for a crashed run (error_category={category!r})"
        )


def test_driver_no_verify_command_is_needs_human(monkeypatch, tmp_path):
    """With no injected gate AND no configured verify/ci command -> needs_human."""
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    config = DispatchConfig()
    config.commands.verify_loop = ""
    config.commands.ci = ""  # nothing to verify against
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    # No gate injected -> driver builds the production gate from config.
    result = DelegateDriver(store, config=config).run(loop)
    assert result.status == "needs_human"
    assert "no verify command configured" in result.status_reason


def test_driver_nonzero_exit_empty_category_fails_closed(monkeypatch, tmp_path):
    """A nonzero exit_code with an EMPTY error_category must fail closed.

    An agent can exit nonzero without a classified category; such a run must not
    reach the gate (a clean worktree would otherwise pass and stamp achieved).
    """
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path, exit_code=1)  # error_category="" by default
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
    assert result.status == "needs_human"
    assert gate.calls == []  # a failed run never reaches the gate


def test_driver_zero_steps_fails_closed_to_needs_human(monkeypatch, tmp_path):
    """A clean run (exit 0, no error) that produced ZERO steps must NOT reach the
    gate — a passing gate on an unchanged worktree would falsely stamp achieved."""
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)
    # _patch_run writes a 2-step event stream; this override SUPERSEDES it so the
    # driver sees zero steps (the written file is then irrelevant to _decide).
    monkeypatch.setattr("kew.loops.delegate._read_steps", lambda event_file: [])

    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)

    assert result.status == "needs_human"
    assert "no steps" in result.status_reason
    assert gate.calls == []  # the gate must never be consulted for a 0-step run


def test_driver_publication_error_fails_closed(monkeypatch, tmp_path):
    """A canonical publication failure must stop before the gate."""
    from kew import dispatch_core
    from kew.config import DispatchConfig

    _patch_run(monkeypatch, tmp_path)  # clean run (exit 0, empty category)

    def _raise(*_args, **_kwargs):
        raise PublicationError("push", "publication unavailable")

    monkeypatch.setattr(dispatch_core, "_push_and_open_pr", _raise)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
    assert result.status == "needs_human"
    assert "publication unavailable" in result.status_reason
    assert gate.calls == []


def test_driver_budget_watchdog_passes_check_and_audits(monkeypatch, tmp_path):
    """The driver builds a budget_check closure from the loop and emits a
    loop_budget_exceeded audit on the budget_limited transition."""
    from kew.config import DispatchConfig
    from kew.runners.registry import ResolvedRoute

    event_file = tmp_path / "run.events.jsonl"
    _write_event_stream(event_file)
    captured = {}

    def _fake_run_tracked_agent(**kwargs):
        captured["budget_check"] = kwargs.get("budget_check")
        return DispatchResult(
            exit_code=1,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="budget_exceeded",
            budget_exceeded=True,
            run_id="real-run-id",
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run_tracked_agent)

    class _Runner:
        name = "claude-code"
        loop_style = "native"

    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda config, complexity=None: ResolvedRoute(
            runner=_Runner(), model="claude-sonnet-4-6"
        ),
    )
    monkeypatch.setattr(
        "kew.loops.delegate._prepare_worktree",
        lambda loop, config, **_kwargs: tmp_path,
    )
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")

    store = SqliteRunStore(":memory:")
    loop = _loop()  # budget_usd=5.0
    loop.spent_usd = 3.0  # prior cumulative spend MUST be included in the cap check
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))

    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)

    assert result.status == "budget_limited"
    bc = captured["budget_check"]
    assert bc is not None
    # prior 3.0 + run cost vs the 5.0 cap — proves prior is added (not hardcoded 0).
    assert bc(3.0) is True  # 3.0 prior + 3.0 > 5.0 cap
    assert bc(1.0) is False  # 3.0 prior + 1.0 < 5.0 cap
    budget_events = [
        e
        for e in store.list_audit_events(limit=None)
        if e.event == "loop_budget_exceeded"
        and isinstance(e.payload, dict)
        and e.payload.get("loop_id") == "L1"
    ]
    assert len(budget_events) == 1
    payload = budget_events[0].payload
    # The budget audit must carry the at-trip figures (regression-pin).
    assert payload["budget_usd"] == 5.0
    assert isinstance(payload["spent_usd"], (int, float))


# ---------------------------------------------------------------------------
# _build_default_gate unit tests (Task 4 — ci_green wiring)
# ---------------------------------------------------------------------------

from kew.loops.delegate import _build_default_gate, _no_gate_reason  # noqa: E402
from kew.loops.verification import (  # noqa: E402
    CiGreenCondition,
    CommandCondition,
    GateEvaluator,
)


def _cfg(**commands) -> DispatchConfig:
    cfg = DispatchConfig()
    for k, v in commands.items():
        setattr(cfg.commands, k, v)
    return cfg


def test_ci_green_mode_builds_ci_green_condition():
    cfg = _cfg(verify_loop_mode="ci_green", ci_poll_timeout_seconds=123)
    cfg.project.repo = "owner/repo"
    gate = _build_default_gate(cfg)
    assert isinstance(gate, GateEvaluator)
    (cond,) = gate._conditions  # single-condition gate
    assert isinstance(cond, CiGreenCondition)
    assert cond._repo == "owner/repo"
    assert cond._timeout_secs == 123


def test_ci_green_without_repo_fails_closed():
    cfg = _cfg(verify_loop_mode="ci_green")
    cfg.project.repo = ""
    assert _build_default_gate(cfg) is None


def test_command_mode_unchanged():
    cfg = _cfg(verify_loop_mode="command", verify_loop="pytest -q")
    gate = _build_default_gate(cfg)
    assert isinstance(gate, GateEvaluator)
    (cond,) = gate._conditions
    assert isinstance(cond, CommandCondition)


def test_no_gate_reason_ci_green_missing_repo():
    cfg = _cfg(verify_loop_mode="ci_green")
    cfg.project.repo = ""
    assert "project.repo" in _no_gate_reason(cfg)


def test_no_gate_reason_command_mode():
    cfg = _cfg(verify_loop_mode="command")
    assert _no_gate_reason(cfg) == "no verify command configured"


def test_run_accounting_from_zero(monkeypatch, tmp_path):
    """A fresh run() sets step_count/spent_usd from this session alone (offsets=0)."""
    import pytest

    from kew.config import DispatchConfig

    # Use the same real SqliteRunStore(":memory:") + _patch_run + _FakeGate(passed=True)
    # harness as the existing achieved-path tests.  Two step events cost 0.01 each.
    event_file = tmp_path / "run.events.jsonl"
    lines = [
        {
            "ts": 1000.0,
            "type": "started",
            "runner": "claude-code",
            "model": "claude-sonnet-4-6",
            "issue": 7,
        },
        {"ts": 1010.0, "type": "step", "input": 100, "output": 20, "cost_usd": 0.01},
        {"ts": 1025.0, "type": "step", "input": 200, "output": 40, "cost_usd": 0.01},
        {
            "ts": 1030.0,
            "type": "finished",
            "exit_code": 0,
            "total_tokens": 360,
            "cost_usd": 0.02,
        },
    ]
    event_file.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n")

    from kew.runners.registry import ResolvedRoute

    class _Runner:
        name = "claude-code"
        loop_style = "native"

    def _fake_run(**kwargs):
        assert kwargs["loop_id"] == "L1"
        return __import__("kew.tracking", fromlist=["DispatchResult"]).DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="",
            run_id="real-run-id",
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run)
    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda config, complexity=None: ResolvedRoute(
            runner=_Runner(), model="claude-sonnet-4-6"
        ),
    )
    monkeypatch.setattr(
        "kew.loops.delegate._prepare_worktree",
        lambda loop, config, **_kwargs: tmp_path,
    )
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")

    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    finished = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)

    assert finished.step_count == 2
    assert finished.spent_usd == pytest.approx(0.02)
    its = store.list_iterations("L1")
    assert [it.idx for it in its] == [0, 1]


# ---------------------------------------------------------------------------
# Task 3: DelegateDriver.resume tests
# ---------------------------------------------------------------------------


def _bind_prior_checkout(
    store: SqliteRunStore, loop: LoopRecord, checkout: Path
) -> None:
    """Seed the fresh-run binding required by C4 before constructing a resume."""
    config = DispatchConfig()
    claim_checkout(checkout)
    try:
        DelegateDriver(store, config=config).bind_checkout(loop, checkout)
    finally:
        release_checkout_claim(checkout)


def _patch_run_for_resume(monkeypatch, tmp_path, *, passed: bool = True) -> None:
    """Like _patch_run but runner has loop_style='native' (for _validate_resume)."""
    from kew.runners.registry import ResolvedRoute

    event_file = tmp_path / "run.events.jsonl"
    _write_event_stream(event_file)

    class _NativeRunner:
        name = "claude-code"
        loop_style = "native"

    def _fake_run_tracked_agent(**kwargs):
        return __import__("kew.tracking", fromlist=["DispatchResult"]).DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="",
            run_id="resume-run-id",
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run_tracked_agent)
    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda config, complexity=None: ResolvedRoute(
            runner=_NativeRunner(), model="claude-sonnet-4-6"
        ),
    )
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")
    monkeypatch.setattr(
        "kew.loops.delegate.dispatch_checkout_path", Mock(return_value=tmp_path)
    )
    monkeypatch.setattr(
        "kew.loops.delegate.reuse_or_restore_worktree",
        lambda **k: tmp_path,
    )

    gate_verdict = GateVerdict(
        passed=passed, evidence="ok" if passed else "ci red", sabotage=False
    )
    return gate_verdict


def _needs_human_loop(store: "SqliteRunStore", tmp_path: Path) -> LoopRecord:
    """A loop already at needs_human with one prior iteration + accounting."""
    loop = LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="#7: do the thing",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        status="needs_human",
        status_reason="gate_failed: CI failed",
        issue_num=7,
        repo="thossullivan/kew",
        budget_usd=10.0,
        wall_clock_secs=600,
        step_count=2,
        spent_usd=0.02,
        max_steps=200,
    )
    store.save_loop(loop)
    _bind_prior_checkout(store, loop, tmp_path)
    # Add one prior iteration so list_iterations returns prior rows.
    from kew.run_store import IterationRecord

    store.add_iteration(
        IterationRecord(
            iteration_id="iter-prior-1",
            loop_id="L1",
            idx=1,
            started_at=datetime(2026, 6, 23, tzinfo=UTC),
            run_id="prior-run-id",
            kind="assistant_turn",
            status="pursuing",
            cost_usd=0.02,
            tokens=300,
            finished_at=datetime(2026, 6, 23, tzinfo=UTC),
        )
    )
    return loop


def test_resume_from_needs_human_runs_and_regates(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.resume(loop, "fix the lint and retry")

    # Status achieved via real gate pass.
    assert result.status == "achieved"
    # The gate must have been called (anti-proxy).
    assert len(gate.calls) == 1
    # loop_resumed audit emitted — look for it in the audit_events table.
    audit_events = store.list_audit_events(event="loop_resumed", limit=None)
    assert len(audit_events) == 1
    # Continuation iterations have idx >= prior count (idx_offset applied).
    its = store.list_iterations("L1")
    new_its = [it for it in its if it.run_id == "resume-run-id"]
    assert len(new_its) == 2
    assert all(it.idx >= 2 for it in new_its), (
        f"Expected idx >= 2, got {[it.idx for it in new_its]}"
    )
    # step_count accumulates: prior(2) + this session(2) == 4.
    assert result.step_count == 4
    # spent_usd accumulates: prior(0.02) + this session(0.01+0.02=0.03) == approx(0.05).
    import pytest as _pytest

    assert result.spent_usd == _pytest.approx(0.02 + 0.03)


def test_resume_budget_limited_applies_bump(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="#7: do the thing",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        status="budget_limited",
        status_reason="budget_exceeded",
        issue_num=7,
        repo="thossullivan/kew",
        budget_usd=10.0,
        wall_clock_secs=600,
        step_count=2,
        spent_usd=0.02,
        max_steps=200,
    )
    store.save_loop(loop)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    _bind_prior_checkout(store, loop, tmp_path)
    result = driver.resume(loop, "continue", budget_usd=25.0)

    # Budget bump applied before run.
    assert loop.budget_usd == 25.0
    # Terminal status comes from gate (passed=True).
    assert result.status == "achieved"


def test_resume_rejects_non_resumable_state(monkeypatch, tmp_path):
    import pytest

    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="#7: do the thing",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        status="achieved",
        issue_num=7,
        repo="thossullivan/kew",
        budget_usd=10.0,
        wall_clock_secs=600,
    )
    store.save_loop(loop)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    with pytest.raises(ValueError, match="resumable"):
        driver.resume(loop, "go")

    # Assert loop was NOT mutated.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.status == "achieved"
    # No loop_resumed audit emitted.
    audit_events = store.list_audit_events(event="loop_resumed", limit=None)
    assert len(audit_events) == 0


def test_resume_does_not_bypass_gate(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=False)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.resume(loop, "please just mark it done")

    # Gate failed -> needs_human; human guidance cannot force achieved.
    assert result.status == "needs_human"


def _unrestorable_checkout(checkout, store, **kwargs):
    require_checkout_claim(checkout)
    [event] = store.list_audit_events(event="loop_checkout_bound", limit=None)
    assert kwargs["expected_generation"] == event.payload["checkout_id"]
    raise RuntimeError("worktree gone")


def test_resume_worktree_unrestorable_leaves_loop_untouched(monkeypatch, tmp_path):
    """Fail-closed ordering: worktree error after validation must not flip loop status.

    reuse_or_restore_worktree is called AFTER _validate_resume but BEFORE the
    status flip.  If it raises, the loop must remain needs_human and no
    loop_resumed audit event must be emitted.
    """
    import pytest

    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    monkeypatch.setattr(
        "kew.loops.delegate.reuse_or_restore_worktree",
        partial(_unrestorable_checkout, tmp_path, store),
    )

    with pytest.raises(RuntimeError, match="worktree gone"):
        driver.resume(loop, "go")

    # Fail-closed: loop never flipped, no resume audit emitted.
    assert store.get_loop("L1").status == "needs_human"
    audit_events = store.list_audit_events(event="loop_resumed", limit=None)
    assert audit_events == []


# ---------------------------------------------------------------------------
# Task 4: pause wired into DelegateDriver
# ---------------------------------------------------------------------------


def _patch_run_paused(monkeypatch, tmp_path, *, extra_steps: int = 0) -> Path:
    """Like _patch_run but run_tracked_agent returns DispatchResult(paused=True).

    ``extra_steps`` inserts additional step events so the total step count can
    exceed loop.max_steps (for the paused-beats-step-cap test).
    """
    from kew.runners.registry import ResolvedRoute
    from kew.tracking import DispatchResult

    event_file = tmp_path / "run.events.jsonl"
    base_lines = [
        {"ts": 1010.0, "type": "step", "input": 100, "output": 20, "cost_usd": 0.01},
        {"ts": 1025.0, "type": "step", "input": 200, "output": 40, "cost_usd": 0.02},
    ]
    extra_lines = [
        {"ts": 1030.0 + i, "type": "step", "input": 50, "output": 10, "cost_usd": 0.005}
        for i in range(extra_steps)
    ]
    all_lines = base_lines + extra_lines
    event_file.write_text("\n".join(json.dumps(ln) for ln in all_lines) + "\n")

    class _Runner:
        name = "claude-code"
        loop_style = "native"

    captured: dict[str, object] = {}

    def _fake_run_tracked_agent(**kwargs):
        captured["should_pause"] = kwargs.get("should_pause")
        return DispatchResult(
            exit_code=1,  # SIGTERM'd agent exits nonzero
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="",
            run_id="paused-run-id",
            paused=True,
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run_tracked_agent)
    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda config, complexity=None: ResolvedRoute(
            runner=_Runner(), model="claude-sonnet-4-6"
        ),
    )
    monkeypatch.setattr(
        "kew.loops.delegate._prepare_worktree",
        lambda loop, config, **_kwargs: tmp_path,
    )
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")
    return event_file, captured


def test_paused_result_finalizes_paused_without_gating(monkeypatch, tmp_path):
    """Paused DispatchResult -> status 'paused'; gate NOT called; loop_paused audit."""
    from kew.config import DispatchConfig

    _patch_run_paused(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    # Simulate a loop that had pause_requested set (the real scenario: operator
    # requested a pause mid-run, so the DB flag and in-memory flag are both True
    # at finalize time).
    loop.pause_requested = True
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.run(loop)

    assert result.status == "paused"
    # Gate must NOT be called — paused short-circuits before gate.
    assert gate.calls == []
    # loop_paused audit event must be emitted.
    audit_events = store.list_audit_events(event="loop_paused", limit=None)
    assert len(audit_events) == 1
    # DB clear must survive save_loop: natural-finish-wins invariant.
    persisted = store.get_loop(loop.loop_id)
    assert persisted is not None
    assert persisted.pause_requested is False  # DB clear must survive save_loop


def test_resume_from_paused(monkeypatch, tmp_path):
    """Resume from 'paused' works: status -> achieved, clear_pause was called."""
    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    # Create a loop at 'paused' with pause_requested set.
    loop = LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="#7: do the thing",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        status="paused",
        status_reason="paused by operator",
        issue_num=7,
        repo="thossullivan/kew",
        budget_usd=10.0,
        wall_clock_secs=600,
        step_count=2,
        spent_usd=0.02,
        max_steps=200,
        pause_requested=True,
    )
    store.save_loop(loop)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    _bind_prior_checkout(store, loop, tmp_path)
    result = driver.resume(loop, "continue from here")

    # paused is resumable -> should transition and achieve.
    assert result.status == "achieved"
    # clear_pause was called: pause_requested must be False after resume.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.pause_requested is False


def test_should_pause_reads_loop_flag(monkeypatch, tmp_path):
    """_should_pause returns True when flag set, False (not crash) when loop is None."""
    from kew.config import DispatchConfig

    _event_file, captured = _patch_run_paused(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    # Run so the should_pause lambda is captured.
    driver.run(loop)

    should_pause = captured.get("should_pause")
    assert should_pause is not None, "should_pause was not passed to run_tracked_agent"

    # Set pause_requested directly via a second loop in pursuing status (the
    # request_pause guard requires status='pursuing', so we use a fresh loop).
    loop2 = LoopRecord(
        loop_id="L2",
        source="issue",
        goal_text="test",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        status="pursuing",
        issue_num=8,
        repo="thossullivan/kew",
        budget_usd=5.0,
        wall_clock_secs=600,
    )
    store.save_loop(loop2)
    store.request_pause("L2")
    # Verify _should_pause reads the flag directly from the driver.
    assert driver._should_pause("L2") is True

    # When get_loop returns None (unknown loop_id), must return False, not crash.
    assert driver._should_pause("nonexistent-loop-id") is False


def test_paused_beats_step_cap(monkeypatch, tmp_path):
    """Paused result with > max_steps still maps to 'paused', not 'needs_human'."""
    from kew.config import DispatchConfig

    # 2 base steps + 3 extra = 5 steps total; loop.max_steps = 1 -> step-cap would fire.
    _patch_run_paused(monkeypatch, tmp_path, extra_steps=3)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    loop.max_steps = 1  # step-cap: 5 steps > 1
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.run(loop)

    # paused MUST win over the step-cap (first check in _decide).
    assert result.status == "paused"


# ---------------------------------------------------------------------------
# Task 1 (Slice 3): runner/model override on resume (#304)
# ---------------------------------------------------------------------------


def _patch_run_for_override(monkeypatch, tmp_path) -> dict:
    """Dedicated capturing fake for run_tracked_agent that records runner.name + model.

    Unlike _patch_run_for_resume, this does NOT monkeypatch resolve_runner (the
    --runner override bypasses it and goes through the real _get_runner_factories()).
    It also captures kwargs["runner"].name and kwargs["model"] for assertion.
    """
    from kew.tracking import DispatchResult

    event_file = tmp_path / "run.events.jsonl"
    _write_event_stream(event_file)

    captured: dict = {}

    def _fake_run_tracked_agent(**kwargs):
        captured["runner_name"] = kwargs["runner"].name
        captured["model"] = kwargs["model"]
        return DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="",
            run_id="override-run-id",
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run_tracked_agent)
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")
    monkeypatch.setattr(
        "kew.loops.delegate.dispatch_checkout_path", Mock(return_value=tmp_path)
    )
    monkeypatch.setattr(
        "kew.loops.delegate.reuse_or_restore_worktree",
        lambda **k: tmp_path,
    )
    return captured


def test_resume_runner_override_reassigns_and_audits(monkeypatch, tmp_path):
    """Override runner_name='opencode' + model='claude-sonnet-4-6' on resume.

    - loop.runner_name updates to 'opencode'
    - run_tracked_agent receives the opencode runner and the override model
    - a 'loop_reassigned' audit is emitted with old_runner/new_runner/model
    """
    from kew.config import DispatchConfig

    captured = _patch_run_for_override(monkeypatch, tmp_path)
    preflighted: list[str | None] = []
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: preflighted.append(runner_name) or None,
    )
    store = SqliteRunStore(":memory:")
    # Loop seeded with runner_name="claude-code"; gate passed=True.
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.resume(
        loop, "go", runner_name="opencode", model="claude-sonnet-4-6"
    )

    # Loop mutated to new runner.
    assert loop.runner_name == "opencode"
    assert result.status == "achieved"
    # run_tracked_agent received the override runner and model.
    assert captured["runner_name"] == "opencode"
    assert captured["model"] == "claude-sonnet-4-6"
    assert preflighted == [captured["runner_name"]]
    # loop_reassigned audit emitted.
    reassigned_events = store.list_audit_events(event="loop_reassigned", limit=None)
    assert len(reassigned_events) == 1
    payload = reassigned_events[0].payload
    assert payload["old_runner"] == "claude-code"
    assert payload["new_runner"] == "opencode"
    assert payload["model"] == "claude-sonnet-4-6"


def test_resume_preflights_the_recorded_runner_not_the_default(monkeypatch, tmp_path):
    from kew.config import DispatchConfig, RouteEntry, RoutingConfig
    from kew.runners.registry import ResolvedRoute

    class _LaunchableCodex:
        name = "codex"
        loop_style = "native"
        publication_style = "agent"

    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6")
        )
    )
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    route = ResolvedRoute(_LaunchableCodex(), "gpt-5.6-sol")
    monkeypatch.setattr(
        "kew.loops.delegate._resolve_resume_route", lambda *a, **k: route
    )
    monkeypatch.setattr(
        "kew.loops.delegate._require_loop_transport_admission", lambda *a: None
    )
    refusal = "Runner 'codex' is not logged in. Authenticate with: codex login"
    preflighted: list[str | None] = []
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: (
            preflighted.append(runner_name)
            or (refusal if runner_name == "codex" else None)
        ),
    )
    launch = MagicMock()
    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", launch)
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=config,
    )

    with pytest.raises(ValueError, match="codex login"):
        driver.resume(loop, "go")

    assert preflighted == ["codex"]
    launch.assert_not_called()
    assert loop.status == "needs_human"


def test_resume_no_override_unchanged(monkeypatch, tmp_path):
    """resume() with no runner_name: loop unchanged, no loop_reassigned audit."""
    from kew.config import DispatchConfig

    _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.resume(loop, "go")

    # Runner unchanged.
    assert loop.runner_name == "claude-code"
    assert result.status == "achieved"
    # No loop_reassigned audit.
    reassigned_events = store.list_audit_events(event="loop_reassigned", limit=None)
    assert len(reassigned_events) == 0


def test_resume_unknown_runner_rejected(monkeypatch, tmp_path):
    """resume() with an unregistered runner_name raises ValueError('unknown runner')."""
    import pytest

    from kew.config import DispatchConfig

    _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    with pytest.raises(ValueError, match="unknown runner"):
        driver.resume(loop, "go", runner_name="not-a-runner")

    # Loop untouched (still needs_human), no loop_reassigned audit.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.status == "needs_human"
    reassigned_events = store.list_audit_events(event="loop_reassigned", limit=None)
    assert len(reassigned_events) == 0


def test_resume_same_runner_no_reassign_audit(monkeypatch, tmp_path):
    """Same runner_name as current: run proceeds but no loop_reassigned audit."""
    from kew.config import DispatchConfig

    _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    # Override with same runner + priced model — no reassign expected.
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.resume(
        loop, "go", runner_name="claude-code", model="claude-sonnet-4-6"
    )

    assert result.status == "achieved"
    # No loop_reassigned audit (same runner, not a change).
    reassigned_events = store.list_audit_events(event="loop_reassigned", limit=None)
    assert len(reassigned_events) == 0


# ---------------------------------------------------------------------------
# #1111: the loop honours the route it is handed (dispatch resolves [routing])
# ---------------------------------------------------------------------------


def test_driver_runs_the_supplied_route_instead_of_the_config_default(
    monkeypatch, tmp_path
):
    """A caller-supplied route wins over ``resolve_runner(config)``.

    dispatch --loop resolves through [routing]; the driver must not re-resolve
    the agent default underneath it and run a different model (#1111).
    """
    from kew.config import DispatchConfig
    from kew.runners.registry import ResolvedRoute

    event_file = _patch_run(monkeypatch, tmp_path)

    class _RoutedRunner:
        name = "opencode"
        loop_style = "native"

    launched: dict = {}

    def _spy_run_tracked_agent(**kwargs):
        launched["runner"] = kwargs["runner"]
        launched["model"] = kwargs["model"]
        return DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="",
            run_id="real-run-id",
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _spy_run_tracked_agent)

    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    route = ResolvedRoute(runner=_RoutedRunner(), model="claude-sonnet-4-6")
    result = driver.run(loop, route=route)

    assert result.status == "achieved"
    assert launched["runner"].name == "opencode"
    assert launched["model"] == "claude-sonnet-4-6"


def test_driver_fails_closed_when_the_route_runner_is_not_native(monkeypatch, tmp_path):
    """A non-native routed runner cannot drive a loop -> needs_human, no run."""
    from kew.config import DispatchConfig
    from kew.runners.registry import ResolvedRoute

    _patch_run(monkeypatch, tmp_path)

    class _NonNativeRunner:
        name = "not-native"
        loop_style = "delegated"

    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.run(
        loop, route=ResolvedRoute(runner=_NonNativeRunner(), model="claude-sonnet-4-6")
    )

    assert result.status == "needs_human"
    assert "not-native" in result.status_reason
    assert "native" in result.status_reason
    assert gate.calls == [], "a rejected route must never reach the gate"


def test_driver_persists_both_halves_of_the_route_on_the_loop(monkeypatch, tmp_path):
    """The routed (runner, model) is written to the loop record, not just held.

    #1111 follow-up: runner_name alone left the route unreconstructable by any
    later actor (resume, reconcile, a second process), so the routing decision
    survived only as an in-memory argument on one CLI call.
    """
    from kew.config import DispatchConfig
    from kew.runners.registry import ResolvedRoute

    _patch_run(monkeypatch, tmp_path)

    class _RoutedRunner:
        name = "opencode"
        loop_style = "native"

    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    driver.run(
        loop, route=ResolvedRoute(runner=_RoutedRunner(), model="claude-sonnet-4-6")
    )

    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.runner_name == "opencode"
    assert persisted.model == "claude-sonnet-4-6"


def test_resume_continues_on_the_loops_recorded_route(monkeypatch, tmp_path):
    """A routed loop resumes on its OWN recorded route, never the config default.

    #1111 regression: resume re-resolved ``resolve_runner(config)``, so from its
    SECOND session on a complexity-routed loop silently reverted to
    ``[routing].default``/``[agent]`` — re-introducing the escalation the first
    session had just avoided, while still naming the original runner.
    """
    from kew.config import DispatchConfig

    captured = _patch_run_for_override(monkeypatch, tmp_path)

    def _explode(*args, **kwargs):
        raise AssertionError("resume must not re-resolve the config route")

    monkeypatch.setattr("kew.loops.delegate.resolve_runner", _explode)

    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.runner_name = "opencode"
    loop.model = "claude-sonnet-4-6"
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    result = driver.resume(reloaded, "go")

    assert result.status == "achieved"
    assert captured["runner_name"] == "opencode"
    assert captured["model"] == "claude-sonnet-4-6"
    # Still recorded after the continuation -> a third session routes the same.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert (persisted.runner_name, persisted.model) == ("opencode", "claude-sonnet-4-6")


def test_resume_falls_back_to_config_route_for_a_pre_v35_loop(monkeypatch, tmp_path):
    """A loop that recorded no model (pre-v35) resumes on the config route."""
    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)  # model == "" (never recorded)
    assert loop.model == ""
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    result = driver.resume(loop, "go")

    assert result.status == "achieved"
    # Resolved from config, and now recorded so the NEXT resume is not a guess.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.model == "claude-sonnet-4-6"


def test_resume_model_override_is_persisted(monkeypatch, tmp_path):
    """A --model swap on resume is recorded, so the next resume keeps it."""
    from kew.config import DispatchConfig

    _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    driver.resume(loop, "go", model="claude-sonnet-4-6")

    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.model == "claude-sonnet-4-6"


def test_resume_records_the_runner_it_actually_ran_not_the_stale_one(
    monkeypatch, tmp_path
):
    """The config-fallback path writes BOTH halves of the route it ran.

    Round-2 regression: resume persisted only ``loop.model``. A pre-v35 loop
    naming a different runner than the config route therefore ended up as a
    (stale runner_name, fresh model) pair no session had ever run — and the NEXT
    resume read that fabricated pair as authoritative and executed it, silently
    switching runners with no operator action and no audit trail.
    """
    from kew.config import DispatchConfig

    gate_verdict = _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.runner_name = "opencode"  # pre-v35: a runner, but no recorded model
    loop.model = ""
    store.save_loop(loop)
    gate = _FakeGate(gate_verdict)
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    result = driver.resume(reloaded, "go")

    assert result.status == "achieved"
    # The config route (claude-code / claude-sonnet-4-6) actually ran, so BOTH
    # halves of it are what the record must now name.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert (persisted.runner_name, persisted.model) == (
        "claude-code",
        "claude-sonnet-4-6",
    )
    # A runner change is a route change: auditable even without a --runner flag.
    reassigned = store.list_audit_events(event="loop_reassigned", limit=None)
    assert len(reassigned) == 1
    assert reassigned[0].payload["old_runner"] == "opencode"
    assert reassigned[0].payload["new_runner"] == "claude-code"


def test_resume_runner_swap_keeps_a_model_the_new_runner_can_run(monkeypatch, tmp_path):
    """``--runner X`` without ``--model`` inherits a model X can actually drive.

    Round-2 regression: the override fell back to ``[agent].model`` instead of
    the loop's own model, contradicting the record-is-authority contract — and
    since resume now writes the model durably, the substituted model poisoned
    every later session (a hard "not priced" refusal when [agent].model is unset,
    as it is on this repo). Inheritance is the right answer *when the pair is
    valid*; see the cross-provider test below for when it is not.
    """
    from kew.config import DispatchConfig

    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.model = "ollama/qwen3"  # opencode's namespace -> a valid swap target
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())
    assert driver._config.agent.model == "", "guard: [agent].model must be unset here"

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    result = driver.resume(reloaded, "go", runner_name="opencode")

    assert result.status == "achieved"
    assert captured["runner_name"] == "opencode"
    assert captured["model"] == "ollama/qwen3"
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert (persisted.runner_name, persisted.model) == ("opencode", "ollama/qwen3")


def test_resume_runner_swap_fails_closed_on_a_cross_provider_model(
    monkeypatch, tmp_path
):
    """``--runner X`` may not inherit a model X cannot run (operator ruling).

    Round-3 regression: inheriting the recorded model unconditionally minted a
    cross-provider route (claude-code + openrouter/...) that passed the priced
    check, was handed to the runner, and was written durably — so every later
    resume repeated it. Neither substitution direction is allowed silently: the
    operator must name both halves.
    """
    import pytest

    from kew.config import DispatchConfig

    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.runner_name = "opencode"
    loop.model = "openrouter/deepseek/deepseek-v4-flash"
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    config = DispatchConfig()
    # Priced, so the existing R2 cost gate would wave the minted route through —
    # this check, not that one, has to be what stops it.
    config.pricing.overrides["openrouter/deepseek/deepseek-v4-flash"] = [0.24, 0.38]
    driver = DelegateDriver(store, gate=gate, config=config)

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    with pytest.raises(ValueError) as excinfo:
        driver.resume(reloaded, "go", runner_name="claude-code")

    message = str(excinfo.value)
    assert "cannot run" in message, "must refuse on compatibility, not on price"
    assert "claude-code" in message, "the refusal must name the runner half"
    assert "openrouter/deepseek/deepseek-v4-flash" in message, "and the model half"
    assert "--model" in message, "and how to proceed deliberately"
    # Fail-closed: nothing ran and nothing was mutated on the record.
    assert captured == {}
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.status == "needs_human"
    assert (persisted.runner_name, persisted.model) == (
        "opencode",
        "openrouter/deepseek/deepseek-v4-flash",
    )


def test_resume_runner_swap_with_an_explicit_model_is_allowed(monkeypatch, tmp_path):
    """An explicit ``--model`` IS the operator naming both halves — no refusal.

    The compatibility check exists to stop kew inventing a route; it must not
    stop an operator from stating one.
    """
    from kew.config import DispatchConfig

    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.runner_name = "opencode"
    loop.model = "openrouter/deepseek/deepseek-v4-flash"
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=DispatchConfig())

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    result = driver.resume(
        reloaded, "go", runner_name="claude-code", model="claude-sonnet-4-6"
    )

    assert result.status == "achieved"
    assert (captured["runner_name"], captured["model"]) == (
        "claude-code",
        "claude-sonnet-4-6",
    )


def test_resume_runner_swap_refusal_names_the_configured_model_too(
    monkeypatch, tmp_path
):
    """The same rule covers the pre-v35 fallback to ``[agent].model``.

    A loop with no recorded model still must not be paired with a config model
    the swapped-in runner cannot run — the mint is identical, only the source
    of the model differs.
    """
    import pytest

    from kew.config import DispatchConfig

    _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.model = ""  # pre-v35: no recorded model
    store.save_loop(loop)
    config = DispatchConfig()
    config.agent.model = "claude-sonnet-4-6"
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=config,
    )

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    with pytest.raises(ValueError, match="claude-sonnet-4-6"):
        driver.resume(reloaded, "go", runner_name="opencode")


def test_resume_unknown_runner_still_reports_the_unknown_runner(monkeypatch, tmp_path):
    """A typo'd ``--runner`` reports the runner, not a model-compatibility error."""
    import pytest

    from kew.config import DispatchConfig

    _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _needs_human_loop(store, tmp_path)
    loop.model = "claude-sonnet-4-6"
    store.save_loop(loop)
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=DispatchConfig(),
    )

    reloaded = store.get_loop("L1")
    assert reloaded is not None
    with pytest.raises(ValueError, match="unknown runner"):
        driver.resume(reloaded, "go", runner_name="opencocde")


# ---------------------------------------------------------------------------
# #1111 round 3: a mid-flight DB route override outranks the recorded route
# ---------------------------------------------------------------------------


def _raise_route_override(store: "SqliteRunStore", field: str, value: object) -> None:
    """Raise a ``config_overrides`` row the way the /config surface does (#659)."""
    store.set_config_override(
        field=field,
        value_json=json.dumps(value),
        file_value_json="null",
        set_at=datetime(2026, 7, 26, tzinfo=UTC).isoformat(),
        set_by="test-operator",
    )


def _simple_tier_config(model: str) -> DispatchConfig:
    """A config whose ``simple`` tier routes to *model* and default does not."""
    return DispatchConfig(
        routing={
            "default": {"runner": "claude-code", "model": "claude-opus-5"},
            "simple": {"runner": "claude-code", "model": model},
        }
    )


def _routed_loop(
    store: "SqliteRunStore", tmp_path: Path, **fields: object
) -> LoopRecord:
    """A resumable loop with a recorded route + tier, reloaded from the store."""
    loop = _needs_human_loop(store, tmp_path)
    loop.model = "claude-sonnet-4-6"
    loop.complexity = "simple"
    for key, value in fields.items():
        setattr(loop, key, value)
    store.save_loop(loop)
    reloaded = store.get_loop(loop.loop_id)
    assert reloaded is not None
    return reloaded


def test_flagless_resume_honours_a_db_route_override(monkeypatch, tmp_path):
    """A route override raised mid-flight steers the next session (#659, O1/#892).

    The loop's recorded route is the resume authority only until a SECOND actor
    uses the runtime-override surface (the web /config page, ``kew config set``)
    — whose entire purpose is steering the effective route without a commit. If
    the record outranked it, an in-flight loop could not be steered at all short
    of killing and re-dispatching it.
    """
    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _routed_loop(store, tmp_path)
    # The operator drops the simple tier to a cheaper model. `load_config(store=)`
    # has already merged that row into the config the driver is handed.
    _raise_route_override(
        store, "routing.simple", {"runner": "claude-code", "model": "claude-haiku-4-5"}
    )
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=_simple_tier_config("claude-haiku-4-5"),
    )

    result = driver.resume(loop, "go")

    assert result.status == "achieved"
    assert captured["model"] == "claude-haiku-4-5", (
        "a DB route override must steer the continuation, not the stale record"
    )
    # ...and the route that ran is what the record now names.
    persisted = store.get_loop("L1")
    assert persisted is not None
    assert persisted.model == "claude-haiku-4-5"


def test_override_resume_re_resolves_at_the_loops_own_tier(monkeypatch, tmp_path):
    """Re-resolving an override uses the loop's recorded tier, not the default.

    Resolving without the tier is the #1111 escalation by another door: a
    simple-tier loop would land on ``[routing].default`` (here ``claude-opus-5``)
    the moment any route override existed.
    """
    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _routed_loop(store, tmp_path)
    _raise_route_override(store, "agent.model", "claude-haiku-4-5")
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=_simple_tier_config("claude-haiku-4-5"),
    )

    result = driver.resume(loop, "go")

    assert result.status == "achieved"
    assert captured["model"] == "claude-haiku-4-5"


def test_flagless_resume_ignores_an_override_on_another_tier(monkeypatch, tmp_path):
    """An override on a tier this loop was not routed at is not its business.

    Only ``agent.model``, ``routing.default`` and the loop's OWN tier entry can
    change its route decision; anything else leaves the recorded route standing.
    """
    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _routed_loop(store, tmp_path)
    _raise_route_override(
        store, "routing.complex", {"runner": "claude-code", "model": "claude-haiku-4-5"}
    )
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=_simple_tier_config("claude-haiku-4-5"),
    )

    result = driver.resume(loop, "go")

    assert result.status == "achieved"
    assert captured["model"] == "claude-sonnet-4-6"  # the recorded route


def test_flagless_resume_without_any_override_keeps_the_recorded_route(
    monkeypatch, tmp_path
):
    """No override row -> a plain kew.toml edit does NOT re-route a live loop.

    The route continuity #1111 bought is only yielded to the explicit override
    surface; a committed config change mid-flight still does not escalate a
    running loop.
    """
    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _routed_loop(store, tmp_path)
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=_simple_tier_config("claude-haiku-4-5"),
    )

    result = driver.resume(loop, "go")

    assert result.status == "achieved"
    assert captured["model"] == "claude-sonnet-4-6"


def test_explicit_model_flag_still_beats_a_db_route_override(monkeypatch, tmp_path):
    """``--model`` is the operator naming the route for THIS loop; it wins.

    The override surface sets a fleet-level route; the flag is a per-loop
    decision made after seeing it, so it must not be silently overruled.
    """
    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _routed_loop(store, tmp_path)
    _raise_route_override(
        store, "routing.simple", {"runner": "claude-code", "model": "claude-haiku-4-5"}
    )
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=_simple_tier_config("claude-haiku-4-5"),
    )

    result = driver.resume(loop, "go", model="claude-opus-5")

    assert result.status == "achieved"
    assert captured["model"] == "claude-opus-5"


def test_pre_v35_loop_with_a_tier_resolves_at_that_tier(monkeypatch, tmp_path):
    """The config fallback for a record with no model also honours its tier."""
    captured = _patch_run_for_override(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _routed_loop(store, tmp_path, model="")
    driver = DelegateDriver(
        store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        config=_simple_tier_config("claude-haiku-4-5"),
    )

    result = driver.resume(loop, "go")

    assert result.status == "achieved"
    assert captured["model"] == "claude-haiku-4-5"  # the tier, not the default


def test_build_loop_from_issue_records_the_routed_tier():
    """The tier the route was resolved at is durable state on the record."""
    loop = build_loop_from_issue(
        {"number": 7, "title": "t", "body": "b"},
        DispatchConfig(),
        runner_name="claude-code",
        model="claude-haiku-4-5",
        complexity="simple",
    )

    assert loop.complexity == "simple"
    assert loop.model == "claude-haiku-4-5"


@pytest.fixture(autouse=True)
def _loops_use_canonical_publication(monkeypatch):
    """Keep unit tests on canonical mode without using a real remote."""
    from kew import dispatch_core

    publication = CanonicalPublication(
        commit_sha="b" * 40,
        tree_sha="c" * 40,
        parent_shas=("a" * 40,),
        changed_paths=("src/foo.py",),
        flags=(),
    )
    monkeypatch.setattr(dispatch_core, "_trusted_dispatch_base", lambda _c: "a" * 40)
    monkeypatch.setattr(dispatch_core, "bind_publication_lineage", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_core, "_git_push_branch", lambda *a, **k: publication)
    monkeypatch.setattr(
        "kew.loops.delegate.publication_remote",
        lambda _repo: "https://example.invalid/repo.git",
    )

    # The publication the PR opener reports; a test swaps in its own.
    published = [publication]

    def _open(*_args, **kwargs):
        callback = kwargs.get("on_publication")
        if callback is not None:
            callback(published[0])
        return "https://example.invalid/pr/7"

    monkeypatch.setattr(dispatch_core, "_push_and_open_pr", _open)
    return published


class _MetCondition:
    """A met condition that executes nothing, so no host admission is needed."""

    requires_uncontained_host_execution = False

    def check(self, **_kwargs: object) -> object:
        from kew.loops.verification import ConditionResult

        return ConditionResult(met=True, evidence="3 tests green")


def _run_with_acceptance_edit(
    monkeypatch, tmp_path, published: list[CanonicalPublication], contract_path: str
):
    """Run a loop whose publication edits the node's recorded acceptance file."""
    from kew.loops.verification import GateEvaluator

    _patch_run(monkeypatch, tmp_path)
    published[0] = CanonicalPublication(
        commit_sha="b" * 40,
        tree_sha="c" * 40,
        parent_shas=("a" * 40,),
        changed_paths=("src/foo.py", contract_path),
        flags=(),
    )
    store = SqliteRunStore(":memory:")
    store.record_acceptance_contract(
        "github:thossullivan/kew#2477",
        "7",
        path=contract_path,
        sha256="d" * 64,
        author_run_id="author-run",
        commit="e" * 40,
    )
    loop = _loop()
    store.save_loop(loop)
    gate = GateEvaluator([_MetCondition()], guarded_paths=[])
    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)
    [event] = store.list_audit_events(event="verification_checked", limit=None)
    return result, event


def test_editing_the_conventional_acceptance_file_is_sabotage(
    monkeypatch, tmp_path, _loops_use_canonical_publication
):
    """#2729 AC1: an implementer edit to the node's acceptance file is sabotage."""
    path = "kew/tests/acceptance/test_issue_7.py"

    result, event = _run_with_acceptance_edit(
        monkeypatch, tmp_path, _loops_use_canonical_publication, path
    )

    assert result.status == "needs_human"
    assert result.status_reason == f"sabotage: diff edits {path}"
    assert event.payload["sabotage"] is True


def test_editing_a_contract_outside_default_globs_is_sabotage(
    monkeypatch, tmp_path, _loops_use_canonical_publication
):
    """#2729 AC1: the recorded contract path guards on its own, where no
    default glob would reach the file."""
    path = "acceptance/issue_7_contract.py"

    result, event = _run_with_acceptance_edit(
        monkeypatch, tmp_path, _loops_use_canonical_publication, path
    )

    assert result.status == "needs_human"
    assert result.status_reason == f"sabotage: diff edits {path}"
    assert event.payload["sabotage"] is True


def test_an_acceptance_file_left_alone_does_not_trip_the_guard(monkeypatch, tmp_path):
    """The control: a recorded contract the diff never touches passes."""
    from kew.loops.verification import GateEvaluator

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    store.record_acceptance_contract(
        "github:thossullivan/kew#2477",
        "7",
        path="acceptance/issue_7_contract.py",
        sha256="d" * 64,
        author_run_id="author-run",
        commit="e" * 40,
    )
    loop = _loop()
    store.save_loop(loop)
    gate = GateEvaluator([_MetCondition()], guarded_paths=[])

    result = DelegateDriver(store, gate=gate, config=DispatchConfig()).run(loop)

    assert result.status == "achieved"


# ------------------------------------- a prompt configuration refusal (#3022)


def _profile_against_custom_template(
    store: SqliteRunStore, tmp_path: Path, monkeypatch
) -> DispatchConfig:
    """An active dispatch profile plus a custom ``[prompt] template``: the real
    render refuses it, so the loop launch never starts a runner."""
    from kew.audit import Actor
    from kew.prompt import build_prompt
    from kew.service import prompt_profiles as profiles

    alice = Actor("human", "alice")
    config = DispatchConfig()
    config.approval.approvers = ["alice"]
    template = tmp_path / "custom.md.j2"
    template.write_text("{{ issue_body_fenced }}\n")
    config.prompt.template = str(template)
    profiles.propose(
        store, role="dispatch", body="Be terse.", config=config, actor=alice
    )
    profiles.approve(
        store,
        role="dispatch",
        version=1,
        generation=profiles.current_generation(store, "dispatch"),
        config=config,
        actor=alice,
    )
    monkeypatch.setattr("kew.loops.delegate.build_prompt", build_prompt)
    monkeypatch.setattr("kew.loops.delegate.build_repo_map", lambda *_a, **_k: None)
    return config


def _retire_profile(store: SqliteRunStore, config: DispatchConfig) -> None:
    from kew.audit import Actor
    from kew.service import prompt_profiles as profiles

    profiles.retire(
        store,
        role="dispatch",
        generation=profiles.current_generation(store, "dispatch"),
        config=config,
        actor=Actor("human", "alice"),
    )


def test_a_refused_fresh_loop_launch_finalizes_as_needs_human(monkeypatch, tmp_path):
    from kew.loops.delegate import _RESUMABLE
    from kew.prompt_addendum import ProfileAddendumNotRenderedError

    _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    config = _profile_against_custom_template(store, tmp_path, monkeypatch)
    loop = _loop()
    store.save_loop(loop)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=config)

    with pytest.raises(ProfileAddendumNotRenderedError):
        driver.run(loop)

    saved = store.get_loop(loop.loop_id)
    assert saved.status == "needs_human"
    assert saved.status in _RESUMABLE
    assert "dispatch prompt profile v1" in saved.status_reason
    assert store.list_all() == []
    assert gate.calls == []


def test_a_refused_continuation_stays_resumable_until_the_profile_retires(
    monkeypatch, tmp_path
):
    """An active row approved before the block-opener rule refuses the launch;
    retiring it is the whole fix."""
    from support.prompt_profiles import activate_unchecked

    from kew.prompt import build_prompt
    from kew.prompt_addendum import ProfileAddendumRefusedError

    _patch_run_for_resume(monkeypatch, tmp_path, passed=True)
    monkeypatch.setattr("kew.loops.delegate.build_prompt", build_prompt)
    monkeypatch.setattr("kew.loops.delegate.build_repo_map", lambda *_a, **_k: None)
    store = SqliteRunStore(":memory:")
    config = DispatchConfig()
    config.approval.approvers = ["alice"]
    activate_unchecked(store, "Like this:\n\n```bash\nuv run pytest\n```")
    loop = _needs_human_loop(store, tmp_path)
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    driver = DelegateDriver(store, gate=gate, config=config)

    with pytest.raises(ProfileAddendumRefusedError):
        driver.resume(loop, "retry")

    refused = store.get_loop(loop.loop_id)
    assert refused.status == "needs_human"
    assert "dispatch prompt profile v1 is refused" in refused.status_reason
    # The prior accounting survives the refused continuation.
    assert (refused.step_count, refused.spent_usd) == (2, pytest.approx(0.02))
    assert store.list_all() == []
    assert gate.calls == []

    _retire_profile(store, config)
    resumed = DelegateDriver(store, gate=gate, config=config).resume(refused, "retry")

    assert resumed.status == "achieved"
    assert len(gate.calls) == 1
    assert resumed.step_count == 4
